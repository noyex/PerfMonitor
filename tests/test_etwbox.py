import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from perfmon import db, etwbox

HEAD = ("Event Name, Type, Event ID, Version, Channel, Level, Opcode, Task, Keyword, PID, TID, Processor Number, "
        "Instance ID, Parent Instance ID, Activity ID, Related Activity ID, Clock-Time, Kernel(ms), User(ms), User Data")
START_FT = 134375000000000000          # FILETIME of the trace start
BOOT_QPC = 3_000_000_000_000           # QPC ticks at trace start (10 MHz)


def ev(name, typ, opcode, clock, *data, pid="0x00000004", tid="0x00000010"):
    cols = [name, typ, "0", "2", "0", "0", str(opcode), "0", "0x0000000000000000", pid, tid, "0", "", "",
            "{00000000-0000-0000-0000-000000000000}", "", str(clock), "0", "0"]
    return ",  ".join(cols + [str(d) for d in data])


def header(perf_freq=10_000_000, clock_type="0x1"):
    data = [4096, 83951626, 26200, 16, START_FT + 600 * 10**7, 156250, 0, "0x5", 5, 1, 8, 3, 4491, "0xA", "0x7",
            START_FT - 10**12, perf_freq, START_FT, clock_type, 0, '"PerfMonBox"', '"C:\\x.etl"']
    return ev("EventTrace", "Header", 0, START_FT, *data)


def ft(sec):
    """FILETIME at `sec` seconds into the trace."""
    return START_FT + int(sec * 10**7)


def qpc(sec):
    return BOOT_QPC + int(sec * 10**7)


def build_csv(path):
    ndis, nvme = 0xFFFFF80010000000, 0xFFFFF80020000000
    lines = [HEAD, header(),
             ev("Process", "DCStart", 3, ft(0), "0xFFFF1", 1234, 4, 1, 0, "0x0", 0, "S-1-5-18", '"game.exe"', '""'),
             ev("Thread", "DCStart", 3, ft(0), 1234, 77, "0x0"),
             # 90 s before the end: outside the 60 s window
             ev("PerfInfo", "DPC", 68, ft(10) + 900_000, qpc(10), hex(ndis + 0x100)),
             # inside: ndis 3 DPCs (1, 2, 120 us + the shortest-DPC offset), nvme ISR 50 us
             ev("PerfInfo", "DPC", 68, ft(50) + 10 + 10, qpc(50), hex(ndis + 0x100)),
             ev("PerfInfo", "DPC", 68, ft(60) + 10 + 20, qpc(60), hex(ndis + 0x200)),
             ev("PerfInfo", "ThreadDPC", 66, ft(70) + 10 + 1200, qpc(70), hex(ndis + 0x300)),
             ev("PerfInfo", "ISR", 67, ft(80) + 10 + 500, qpc(80), hex(nvme + 0x10), 1, 52, 0),
             ev("PerfInfo", "DPC", 68, ft(81) + 10 + 30, qpc(81), "0x1234"),     # no image: "?"
             ev("DiskIo", "Read", 10, ft(85), 0, "0x43", 1048576, 0, 4096, "0xF1", "0xI", 2_500_000, 77),
             ev("DiskIo", "Write", 11, ft(86), 0, "0x43", 524288, 0, 8192, "0xF1", "0xI", 100_000, 999),
             ev("Image", "DCEnd", 4, ft(100), hex(ndis), "0x100000", 0, 0, 0, 0, 0, 0, 0, 0, 0,
                '"\\SystemRoot\\System32\\drivers\\ndis.sys"'),
             ev("Image", "DCEnd", 4, ft(100), hex(nvme), "0x10000", 0, 0, 0, 0, 0, 0, 0, 0, 0,
                '"\\SystemRoot\\System32\\drivers\\stornvme.sys"'),
             ev("Image", "DCEnd", 4, ft(100), "0x7FF600000000", "0x10000", 1234, 0, 0, 0, 0, 0, 0, 0, 0,
                '"C:\\Games\\game.exe"')]
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")


class ParseTest(unittest.TestCase):
    def test_parse_csv(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.csv")
            build_csv(p)
            s = etwbox.parse_csv(p, last_s=60)
        # durations are relative to the shortest DPC (1 us here): 1/2/120 us -> 0/1/119
        dpc = {r["driver"]: r for r in s["dpc_top"]}
        self.assertEqual(dpc["ndis.sys"]["count"], 3)               # the DPC 90 s before the end is outside
        self.assertEqual(dpc["ndis.sys"]["max_us"], 119)
        self.assertEqual(dpc["ndis.sys"]["sum_us"], 0 + 1 + 119)
        self.assertEqual(dpc["?"]["max_us"], 2)
        self.assertEqual(s["isr_top"], [{"driver": "stornvme.sys", "count": 1, "sum_us": 49, "max_us": 49}])
        self.assertEqual(s["events_lost"], 3)
        self.assertEqual(s["window_s"], 60)
        disk = {r["process"]: r for r in s["disk_top"]}
        self.assertEqual(disk["game.exe"]["read_mb"], 1.0)
        self.assertEqual(disk["game.exe"]["max_ms"], 250.0)
        self.assertEqual(disk["?"]["write_mb"], 0.5)                # thread 999 unknown
        sev, title = etwbox.headline(s)
        self.assertEqual(sev, "warning")                            # disk 250 ms
        self.assertIn("ndis.sys", title)
        self.assertIn("game.exe 250 ms", title)

    def test_headline_long_dpc(self):
        sev, title = etwbox.headline({"dpc_top": [{"driver": "amdkmdag.sys", "count": 9, "sum_us": 9000, "max_us": 6200}]})
        self.assertEqual(sev, "serious")
        self.assertEqual(title, "ETW: DPC: amdkmdag.sys 6.2 ms max")
        self.assertEqual(etwbox.headline({})[1], "ETW: brak długich DPC/ISR")


class BoxTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_wpr(*args, timeout=0):
            self.calls.append(args[0])
            if args[0] == "-stop":
                with open(args[1], "wb") as f:
                    f.write(b"x" * 1024)
            return 0, ""
        p = mock.patch.object(etwbox, "wpr", fake_wpr)
        p.start()
        self.addCleanup(p.stop)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p2 = mock.patch.object(etwbox, "TRACES", etwbox.Path(td.name))
        p2.start()
        self.addCleanup(p2.stop)
        self.captures = []
        self.box = etwbox.EtwBox({}, self.captures.append)
        self.addCleanup(self.box.stop)

    def drain(self):
        self.box.q.join()

    def test_game_mode_start_trigger_cooldown_stop(self):
        self.box.ensure(False)
        self.drain()
        self.assertFalse(self.box.active)
        self.box.ensure(True)
        self.drain()
        self.assertTrue(self.box.active)
        self.assertTrue(self.box.trigger("user_mark", session=7))
        self.assertFalse(self.box.trigger("dwm_stall"))             # cooldown
        self.drain()
        self.assertEqual(len(self.captures), 1)
        c = self.captures[0]
        self.assertEqual((c["trigger"], c["session"], c["status"]), ("user_mark", 7, "captured"))
        self.assertTrue(c["path"].endswith("_user_mark.etl"))
        self.assertTrue(self.box.active)                             # restarted after the dump
        self.box.ensure(False)
        self.drain()
        self.assertFalse(self.box.active)
        self.assertEqual(self.calls, ["-cancel", "-start", "-stop", "-start", "-cancel"])

    def test_burst_and_dpc_triggers(self):
        self.box.active = True
        for i in range(4):
            self.assertFalse(self.box.on_stutter(100.0 + i, 1))
        self.assertTrue(self.box.on_stutter(104.0, 1))
        self.box.last_dump = 0
        self.assertFalse(self.box.on_sample(200.0, 9.0, 1))
        self.assertFalse(self.box.on_sample(201.0, 3.0, 1))          # dropped below: timer resets
        self.assertFalse(self.box.on_sample(202.0, 9.0, 1))
        self.assertTrue(self.box.on_sample(204.0, 9.5, 1))

    def test_disabled_trigger_and_manual_control(self):
        self.box.s["triggers"]["user_mark"] = False
        self.box.active = True
        self.assertFalse(self.box.trigger("user_mark"))
        self.box.control("start")
        self.box.ensure(False)
        self.assertTrue(self.box.want)


class RetentionTest(unittest.TestCase):
    def test_age_and_size_limits(self):
        con = sqlite3.connect(":memory:")
        db.migrate(con)
        with tempfile.TemporaryDirectory() as td:
            now = 10_000_000.0
            for i, (age_days, mb) in enumerate(((20, 10), (5, 1500), (1, 1000))):
                p = os.path.join(td, f"{i}.etl")
                open(p, "wb").close()
                con.execute("INSERT INTO etw_captures(ts, trigger, path, size_mb, status) VALUES (?,?,?,?,?)",
                            (now - age_days * 86400, "user_mark", p, mb, "captured"))
            n = etwbox.retention(con, {"retention_days": 14, "max_gb": 2}, now)
            self.assertEqual(n, 2)                                   # too old, then oldest over 2 GB
            left = [r[0] for r in con.execute("SELECT path FROM etw_captures ORDER BY ts")]
            self.assertEqual([p is None for p in left], [True, True, False])
            self.assertEqual(sorted(os.listdir(td)), ["2.etl"])

    def test_settings_merge(self):
        s = etwbox.settings({"etw": {"mode": "always", "triggers": {"dpc_pct": 0}}})
        self.assertEqual(s["mode"], "always")
        self.assertEqual(s["buffer_mb"], 256)
        self.assertEqual(s["triggers"]["dpc_pct"], 0)
        self.assertTrue(s["triggers"]["user_mark"])


if __name__ == "__main__":
    unittest.main()
