import json
import os
import struct
import tempfile
import unittest
from unittest import mock

from perfmon import db, diagreport, disktemp, dumpconfig, kdumps, memdiag, resetforensics
from perfmon.sensors import HardwareSensors
from tests.test_bottleneck import new_db


class _S:
    def __init__(self, stype, name, value):
        self.SensorType, self.Name, self.Value = stype, name, value


class _H:
    def __init__(self, name, ident, sensors):
        self.Name, self.Identifier, self.Sensors = name, ident, [_S(*s) for s in sensors]


def _hs():
    hw = HardwareSensors.__new__(HardwareSensors)
    hw.storage_temps, hw.storage_hot, hw.storage_limits = {}, {}, {}
    return hw


class StorageTempsTest(unittest.TestCase):
    def test_nvme_composite_hottest_and_limits(self):
        hw = _hs()
        hw._storage_temps(_H("990 PRO", "/nvme/2", [
            ("Temperature", "Composite Temperature", 45.0), ("Temperature", "Temperature #1", 44.9),
            ("Temperature", "Temperature #2", 50.9), ("Temperature", "Warning Temperature", 81.0),
            ("Temperature", "Critical Temperature", 84.0), ("Load", "Used Space", 77.0)]))
        self.assertEqual(hw.storage_temps, {"990 PRO": 45.0})
        self.assertEqual(hw.storage_hot, {"990 PRO": 50.9})
        self.assertEqual(hw.storage_limits, {"990 PRO": {"warn": 81.0, "crit": 84.0}})

    def test_sata_single_temperature(self):
        hw = _hs()
        hw._storage_temps(_H("CX400", "/ssd/1", [("Temperature", "Temperature", 33.0)]))
        self.assertEqual(hw.storage_temps, {"CX400": 33.0})
        self.assertEqual(hw.storage_hot, {})
        self.assertEqual(hw.storage_limits, {})
        self.assertFalse(HardwareSensors._nvme(_H("CX400", "/ssd/1", [])))
        self.assertTrue(HardwareSensors._nvme(_H("990", "/nvme/2", [])))


def _reboot(con, last, boot):
    con.execute("INSERT INTO incidents(ts, kind, severity, title, details) VALUES (?,?,?,?,?)",
                (last, "reboot", "info", "Restart", json.dumps({"last_sample": last, "boot": boot})))


class ResetForensicsTest(unittest.TestCase):
    def setUp(self):
        self.con = new_db()
        self.t1 = 1_000_000.0
        self.con.execute("INSERT INTO sessions(id, exe, start_ts) VALUES (7, 'game.exe', ?)", (self.t1 - 3600,))

    def _fill(self, sag=False, frozen=False, idle=0, gpu_w=280):
        for i in range(600):
            ts = self.t1 - 599 + i
            late = ts > self.t1 - 30
            self.con.execute("INSERT INTO samples(ts, game_session, cpu_total, gpu_load, gpu_power_w, cpu_power_w, cpu_temp, "
                             "gpu_hotspot, dwm_ms, ui_ms, user_idle_s) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (ts, 7, 40, 98, gpu_w, 90, 70, 85, 5000 if frozen and late else 5, 1, idle))
            self.con.execute("INSERT INTO hw_detail(ts, session, v12, v5, v33, v3sb, pch_temp) VALUES (?,?,?,?,?,?,?)",
                             (ts, 7, 11.3 if sag and ts > self.t1 - 5 else 12.1, 5.0, 3.32, 3.29, 45))

    def test_rail_sag_and_quick_restart(self):
        self._fill(sag=True)
        _reboot(self.con, self.t1, self.t1 + 25)
        f = resetforensics.build(self.con, self.t1 + 35)
        self.assertEqual(f["session"], 7)
        self.assertEqual(f["exe"], "game.exe")
        self.assertEqual(f["down_s"], 25)
        self.assertEqual(f["rails"]["v12"]["min"], 11.3)
        self.assertAlmostEqual(f["rails"]["v12"]["baseline"], 12.1)
        self.assertGreater(f["rails"]["v12"]["sag_pct"], 5)
        self.assertTrue(f["rails"]["v12"]["outside_atx"])
        self.assertFalse(f["rails"]["v5"]["outside_atx"])
        self.assertIsNone(f["frozen_s_before"])
        txt = " ".join(f["notes"])
        self.assertIn("+12V spadło do 11.30 V", txt)
        self.assertIn("wstał sam po 25 s", txt)
        self.assertIn("nie dowodzi", txt)   # honest about the 1 s Super I/O readings
        fs = resetforensics.findings([{"forensics": f}])
        self.assertEqual(fs[0]["severity"], "serious")
        self.assertIn("Spadek napięcia", fs[0]["title"])

    def test_frozen_display_with_user_active(self):
        self._fill(frozen=True, gpu_w=60)
        f = resetforensics.build(self.con, self.t1 + 60)   # no reboot incident: last sample within 6 h
        self.assertIsNone(f["down_s"])
        self.assertEqual(f["frozen_s_before"], 29)
        self.assertIn("zawiesiła się grafika", " ".join(f["notes"]))
        self.assertEqual(f["rails"]["v12"]["sag_pct"], 0.0)

    def test_dwm_stall_with_display_asleep_is_not_a_freeze(self):
        self._fill(frozen=True, idle=900, gpu_w=60)
        self.assertIsNone(resetforensics.build(self.con, self.t1 + 60)["frozen_s_before"])

    def test_reboot_incident_finds_last_sample_hours_before(self):
        self._fill()
        _reboot(self.con, self.t1, self.t1 + 20 * 3600)
        f = resetforensics.build(self.con, self.t1 + 20 * 3600 + 10)
        self.assertEqual(f["last_sample_ts"], self.t1)
        self.assertEqual(f["down_s"], 20 * 3600)
        self.assertIn("wyłączony 20.0 h", " ".join(f["notes"]))

    def test_no_data(self):
        self.assertIsNone(resetforensics.build(self.con, self.t1))

    def test_freeze_that_recovered_is_not_counted(self):
        self._fill(frozen=True, gpu_w=60)
        self.con.execute("UPDATE samples SET dwm_ms=5 WHERE ts>?", (self.t1 - 6,))
        self.assertIsNone(resetforensics.build(self.con, self.t1 + 60)["frozen_s_before"])

    def test_constant_calibration_offset_is_not_a_sag(self):
        self._fill()
        self.con.execute("UPDATE hw_detail SET v12=11.38")
        f = resetforensics.build(self.con, self.t1 + 60)
        self.assertTrue(f["rails"]["v12"]["outside_atx"])
        self.assertFalse(f["rails"]["v12"]["sag"])
        self.assertIn("przesunięcie kalibracji", " ".join(f["notes"]))
        self.assertNotIn("spadło", " ".join(f["notes"]))
        self.assertNotEqual(resetforensics.findings([{"forensics": f}])[0]["title"], "Spadek napięcia zasilacza przed resetem")

    def test_system_alive_after_last_sample(self):
        self._fill()
        _reboot(self.con, self.t1, self.t1 + 8 * 3600)
        self.con.execute("INSERT INTO events(ts, log, provider, event_id, level, record_id, message) VALUES (?,?,?,?,?,?,?)",
                         (self.t1 + 3600, "System", "x", 1, 2, 1, "m"))
        f = resetforensics.build(self.con, self.t1 + 8 * 3600 + 10)
        self.assertIsNotNone(f["alive_until"])
        self.assertIsNone(f["down_s"])
        self.assertEqual(len(f["notes"]), 1)
        self.assertEqual(resetforensics.findings([{"forensics": f}]), [])

    def test_fill_never_breaks_health(self):
        u = [{"id": 1, "ts": 5.0, "forensics": None}]
        with mock.patch.object(resetforensics, "build", side_effect=RuntimeError("boom")),                 self.assertLogs("perfmon.resetforensics", "ERROR"):
            resetforensics.fill(self.con, u)
        self.assertIsNone(u[0]["forensics"])


class MemDiagTest(unittest.TestCase):
    def test_session_memory_names_commit_holders_and_pagefile(self):
        con = new_db()
        for i in range(10):
            ts = 100 + i * 5
            con.execute("INSERT INTO samples(ts, commit_gb, commit_limit_gb, ram_pct) VALUES (?,?,?,?)", (ts, 37, 39.6, 70))
            con.executemany("INSERT INTO procs(ts, kind, name, pid, mem_mb, commit_mb) VALUES (?,?,?,?,?,?)",
                            [(ts, "mem", "cod.exe", 1, 6000, 14000), (ts, "cpu", "cod.exe", 1, 6000, 14000),
                             (ts, "commit", "brave.exe", 2, 400, 2000 + i), (ts, "commit", "brave.exe", 3, 400, 1000)])
            con.execute("INSERT INTO mem_samples(ts, standby_mb, compressed_mb, pf_mb, pf_free_gb, pf_grow_mb) "
                        "VALUES (?,?,?,?,?,?)", (ts, 2000, 1100, 8700, 0.8, 0))
        m = memdiag.session_memory(con, 90, 200)
        self.assertEqual([h["name"] for h in m["commit_holders"]], ["cod.exe", "brave.exe"])
        self.assertEqual(m["commit_holders"][0]["commit_max_mb"], 14000)   # the cpu + mem rows of one pid count once
        self.assertEqual((m["commit_holders"][1]["commit_max_mb"], m["commit_holders"][1]["procs_max"]), (3009, 2))
        self.assertEqual(m["commit_peak_pct"], 93.4)
        f = memdiag.findings({"memory": m, "stutters": {"cause_counts": {}}})
        self.assertEqual(len(f), 1)
        self.assertIn("cod.exe 13.7 GB", f[0]["detail"])
        self.assertIn("nie ma miejsca", f[0]["detail"])
        self.assertIn("brave.exe (2 proc.) 2.9 GB", f[0]["detail"])
        self.assertIn("kompresował", f[0]["detail"])

    def test_pagefile_at_its_maximum(self):
        txt = memdiag.pagefile_txt({"pagefile_can_grow_min_mb": 0, "pagefile_volume_free_min_gb": 200,
                                    "pagefile_max_mb": 16384, "pagefile_cap_mb": 16384, "pagefile_drives": "D:"})
        self.assertIn("maksimum", txt)
        txt = memdiag.pagefile_txt({"pagefile_can_grow_min_mb": 0, "pagefile_volume_free_min_gb": 0.8,
                                    "pagefile_max_mb": 8704, "pagefile_cap_mb": 33664, "pagefile_drives": "C:"})
        self.assertIn("zwolnij miejsce na C:", txt)

    def test_no_finding_when_commit_is_fine(self):
        self.assertEqual(memdiag.findings({"memory": {"commit_peak_pct": 60}, "stutters": {}}), [])

    def test_lists_and_mem_row(self):
        pdh = {"standby_core": 2**30, "standby_normal": 2**30, "standby_reserve": None, "modified_bytes": 2**29,
               "free_zero_bytes": 2**31, "pool_np_bytes": 2**28}
        pf = {"files": [{"path": r"C:\pagefile.sys", "size_gb": 8.5}], "size_gb": 8.5, "max_gb": 32.9, "free_gb": 16.3,
              "can_grow_gb": 15.3}
        r = memdiag.mem_row(1.0, 3, pdh, 572.2, pf)
        self.assertEqual((r["pf_max_mb"], r["pf_drives"]), (33690, "C:"))
        self.assertEqual((r["standby_mb"], r["modified_mb"], r["free_mb"], r["pool_np_mb"]), (2048, 512, 2048, 256))
        self.assertEqual((r["pf_mb"], r["pf_grow_mb"], r["compressed_mb"]), (8704, 15667, 572.2))
        self.assertIsNone(memdiag.lists({})["standby_mb"])

    def test_system_managed_cap(self):
        self.assertAlmostEqual(memdiag.system_managed_max_gb(32, 263), 32.875)
        self.assertEqual(memdiag.system_managed_max_gb(32, 2000), 96)


def _dump(path, code, sig=b"PAGEDU64"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        h = bytearray(0x100)
        h[:8] = sig
        struct.pack_into("<I", h, 0x38, code)
        struct.pack_into("<4Q", h, 0x40, 1, 2, 3, 4)
        f.write(h)


class KernelDumpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lkr = os.path.join(self.tmp.name, "LiveKernelReports")
        self.mini = os.path.join(self.tmp.name, "Minidump")
        self.p = mock.patch.multiple(kdumps, LKR=self.lkr, MINIDUMP=self.mini,
                                     MEMORY_DMP=os.path.join(self.tmp.name, "MEMORY.DMP"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_header_and_classification(self):
        w = os.path.join(self.lkr, "WATCHDOG", "WATCHDOG-20261101-2210.dmp")
        _dump(w, 0x141)
        self.assertEqual(kdumps.read_header(w), (0x141, [1, 2, 3, 4]))
        self.assertEqual(kdumps.classify(w, 0x141)[:2], ("gpu", "live"))
        m = os.path.join(self.mini, "111126-1234-01.dmp")
        _dump(m, 0x124)
        self.assertEqual(kdumps.classify(m, 0x124)[:2], ("bsod", "bsod"))
        u = os.path.join(self.lkr, "USBHUB3", "x.dmp")
        self.assertEqual(kdumps.classify(u, None)[0], "usb")
        junk = os.path.join(self.tmp.name, "junk.dmp")
        with open(junk, "wb") as f:
            f.write(b"MDMP")
        self.assertEqual(kdumps.read_header(junk), (None, None))

    def test_scan_once_links_session(self):
        con = new_db()
        w = os.path.join(self.lkr, "WATCHDOG", "WATCHDOG-1.dmp")
        _dump(w, 0x117)
        mt = os.stat(w).st_mtime
        con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts) VALUES (5, 'cod.exe', ?, ?)", (mt - 600, mt + 600))
        self.assertEqual(kdumps.scan(con, now=mt + 5), [])   # may still be written
        new = kdumps.scan(con, now=mt + 120)
        self.assertEqual(len(new), 1)
        sev, title, det, ts = new[0]
        self.assertEqual(sev, "serious")
        self.assertEqual((det["session"], det["exe"], det["when"]), (5, "cod.exe", "during"))
        self.assertEqual(det["bugcheck_name"], "VIDEO_TDR_TIMEOUT_DETECTED")
        self.assertIn("0x117", title)
        self.assertEqual(kdumps.scan(con, now=mt + 400), [])   # remembered
        os.utime(w, (mt + 500, mt + 500))   # rewritten in place = a new dump
        self.assertEqual(len(kdumps.scan(con, now=mt + 900)), 1)
        con.execute("INSERT INTO incidents(ts, kind, severity, title, details) VALUES (?,?,?,?,?)",
                    (ts, "kernel_dump", sev, title, json.dumps(det)))
        f = kdumps.findings(con, 0)
        self.assertIn("timeout sterownika GPU", f[0]["title"])

    def test_no_bsod_dumps_explained(self):
        con = new_db()
        f = kdumps.findings(con, 0, [{"bugcheck_code": 0}], {"problems": ["zapis zrzutów wyłączony (CrashDumpEnabled=0)"]})
        self.assertEqual(f[0]["title"], "Brak zrzutów blue screena przy resetach")
        self.assertIn("CrashDumpEnabled=0", f[0]["detail"])


class DumpConfigTest(unittest.TestCase):
    def test_check(self):
        ram = 32 * 1024
        self.assertEqual(dumpconfig.check({"CrashDumpEnabled": 3}, 8000, True, ram), [])
        self.assertIn("wyłączony", dumpconfig.check({"CrashDumpEnabled": 0}, 8000, True, ram)[0])
        self.assertIn("brak pliku wymiany", dumpconfig.check({"CrashDumpEnabled": 3}, None, False, ram)[0])
        self.assertIn("za mały", dumpconfig.check({"CrashDumpEnabled": 2}, 4096, False, ram)[0])
        self.assertIn("zarządzanego przez system", dumpconfig.check({"CrashDumpEnabled": 7}, 8000, False, ram)[0])


class DiskTempTest(unittest.TestCase):
    def test_hot_game_disk(self):
        con = new_db()
        db.set_meta(con, disktemp.LIMITS_KEY, json.dumps({"Samsung SSD 990 PRO 1TB": {"warn": 81, "crit": 84}}))
        info = {"disk_map": [{"letter": "D", "disk": 0}], "disk_health": [{"device": "0", "name": "Samsung SSD 990 PRO 1TB"}]}
        con.execute("INSERT INTO system_info VALUES (?, ?)", (0, json.dumps(info)))
        for i in range(120):
            t = 60 + (25 if i >= 60 else 0)
            con.execute("INSERT INTO samples(ts, extra) VALUES (?, ?)", (1000 + i, json.dumps(
                {"disks": {}, "disk_temps": {"Samsung SSD 990 PRO 1TB": t, "CX400": 33},
                 "disk_hot": {"Samsung SSD 990 PRO 1TB": t + 4}})))
        d = disktemp.DiskTemp(con, r"D:\Games\cod.exe", 1000, 1120)
        self.assertEqual(d.name, "Samsung SSD 990 PRO 1TB")
        self.assertFalse(d.hot(1010))
        self.assertTrue(d.hot(1100))
        s = d.summary()
        self.assertEqual((s["composite_max_c"], s["hottest_max_c"], s["warn_c"]), (85, 89, 81))
        f = disktemp.findings({"disk_temp": s, "stutters": {"cause_counts": {"disk_hot": 4}}})
        self.assertEqual(f[0]["severity"], "serious")
        self.assertIn("na progu throttlingu", f[0]["title"])

    def test_cool_disk_no_finding(self):
        self.assertEqual(disktemp.findings({"disk_temp": {"hottest_max_c": 50, "hot_threshold_c": 76}}), [])

    def test_stutter_ext_tags_disk_hot(self):
        con = new_db()
        db.set_meta(con, disktemp.LIMITS_KEY, json.dumps({"990": {"warn": 81, "crit": 84}}))
        con.execute("INSERT INTO samples(ts, extra) VALUES (?, ?)", (150, json.dumps({"disk_temps": {"990": 80}})))
        ext = diagreport.StutterExt(con, {"exe": "game.exe", "id": 1, "path": None}, 100, 200)
        self.assertIn("disk_hot", ext.refine(150, [], []))
        self.assertNotIn("disk_hot", ext.refine(100 - 40, [], []))


class MigrationTest(unittest.TestCase):
    def test_new_columns_exist(self):
        con = new_db()
        cols = {r[1] for r in con.execute("PRAGMA table_info(hw_detail)")}
        self.assertTrue({"v12", "v5", "v33", "v3sb", "pch_temp", "pcie_temp", "sys_temp", "gpu_fan_pct"} <= cols)
        self.assertIn("commit_mb", {r[1] for r in con.execute("PRAGMA table_info(procs)")})
        con.execute("SELECT standby_mb, pf_grow_mb FROM mem_samples").fetchall()


if __name__ == "__main__":
    unittest.main()
