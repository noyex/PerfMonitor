import json
import sqlite3
import unittest

from perfmon import diskinfo as di


def hd(device, name="Samsung SSD 990 PRO 2TB", bus="NVMe", **kw):
    return {"device": device, "name": name, "bus": bus, **kw}


class HealthChanges(unittest.TestCase):
    def test_uncorrected_increase(self):
        prev = {"disk_health": [hd(2, read_unc=0, write_unc=0)]}
        cur = {"disk_health": [hd(2, read_unc=3, write_unc=0)]}
        (k, sev, title, det), = di.health_changes(prev, cur)
        self.assertEqual((k, sev), ("disk_health", "serious"))
        self.assertEqual(title, "Dysk Samsung SSD 990 PRO 2TB: przybyło błędów nieskorygowanych (odczyt 0 -> 3)")
        self.assertEqual(det["after"]["read_unc"], 3)

    def test_write_and_read(self):
        prev = {"disk_health": [hd("1", read_unc=1, write_unc=0)]}
        cur = {"disk_health": [hd(1, read_unc=2, write_unc=5)]}
        (_, _, title, _), = di.health_changes(prev, cur)
        self.assertIn("odczyt 1 -> 2, zapis 0 -> 5", title)

    def test_no_change_none_or_missing(self):
        d = {"disk_health": [hd(2, read_unc=3, write_unc=0, wear=10)]}
        self.assertEqual(di.health_changes(d, d), [])
        self.assertEqual(di.health_changes({}, {}), [])
        self.assertEqual(di.health_changes(None, d), [])        # no baseline: errors are not "new"
        nulls = {"disk_health": [hd(2, read_unc=None, write_unc=None, wear=None)]}   # not admin
        self.assertEqual(di.health_changes(d, nulls), [])
        self.assertEqual(di.health_changes(nulls, d), [])
        self.assertEqual(di.health_changes({"disk_health": [hd(5, read_unc=0)]}, d), [])  # other device

    def test_wear_once(self):
        old = {"disk_health": [hd(2, wear=85)]}
        worn = {"disk_health": [hd(2, wear=90)]}
        (_, sev, title, _), = di.health_changes(old, worn)
        self.assertEqual(sev, "serious")
        self.assertIn("90%", title)
        self.assertEqual(di.health_changes(worn, {"disk_health": [hd(2, wear=91)]}), [])
        self.assertEqual(len(di.health_changes({}, worn)), 1)           # prev missing -> report
        self.assertEqual(len(di.health_changes({"disk_health": [hd(2)]}, worn)), 1)   # prev wear unknown
        self.assertEqual(di.health_changes(old, {"disk_health": [hd(2, wear=89)]}), [])


class Parsing(unittest.TestCase):
    def test_raidport(self):
        self.assertEqual(di.raidport_disk(r"Reset to device, \Device\RaidPort0, was issued."), 0)
        self.assertEqual(di.raidport_disk(r"Wystąpił reset urządzenia \Device\RaidPort12, został wysłany."), 12)
        self.assertEqual(di.raidport_disk(r"Reset do urządzenia, \Device\Harddisk3\DR3, został wykonany."), 3)
        self.assertEqual(di.raidport_disk(r"The IO operation at logical block address 0x1a2b for Disk 1 "
                                          r"(PDO name: \Device\0000003a) was retried."), 1)
        self.assertEqual(di.raidport_disk(r"Operację we/wy pod adresem logicznego bloku 0x1a2b dla dysku 1 "
                                          r"(nazwa PDO: \Device\0000003a) ponowiono."), 1)
        self.assertEqual(di.raidport_disk("", [r"\Device\RaidPort2", "x"]), 2)
        self.assertEqual(di.raidport_disk("", ["0x1a2b", r"\Device\Harddisk4\DR4"]), 4)
        self.assertIsNone(di.raidport_disk("nothing here"))
        self.assertIsNone(di.raidport_disk(r"\Device\HarddiskVolume3 something"))
        self.assertIsNone(di.raidport_disk(None))

    def test_ntfs_volume(self):
        f = di.ntfs_volume
        self.assertEqual(f("The file system structure on volume C: is corrupt."), "C:")
        self.assertEqual(f("Wolumin C: jest uszkodzony."), "C:")
        self.assertEqual(f("Struktura systemu plików na woluminie d: jest uszkodzona."), "D:")
        self.assertEqual(f("Run chkdsk on the volume Windows (C:)."), "C:")
        self.assertEqual(f("Corruption may occur in VolumeId: E:, DeviceName: \\Device\\HarddiskVolume3."), "E:")
        self.assertEqual(f("Błąd zapisu", ["E:"]), "E:")
        self.assertEqual(f("Błąd zapisu", ["(F:)"]), "F:")
        self.assertEqual(f(r"Błąd na \Device\HarddiskVolume3 (nazwa)"), r"\Device\HarddiskVolume3")
        self.assertEqual(f("x", [r"\Device\HarddiskVolume7"]), r"\Device\HarddiskVolume7")
        self.assertIsNone(f("nothing"))
        self.assertIsNone(f(None))


def make_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""CREATE TABLE samples (ts REAL, extra TEXT);
        CREATE TABLE game_proc (ts REAL, session INTEGER, io_read_mbs REAL);
        CREATE TABLE system_info (ts REAL, info TEXT);""")
    return c


class SessionDisk(unittest.TestCase):
    def setUp(self):
        c = self.c = make_db()
        for i in range(100):
            ex = {"disks": {"2 C: D:": {"lat_ms": float(i), "rd_mbs": 2.0}, "0 F:": {"lat_ms": 0.5, "rd_mbs": 0.5},
                            "1 E:": {"lat_ms": 1.0, "rd_mbs": 1.0}, "_Total": {"lat_ms": 9, "rd_mbs": 9}}}
            c.execute("INSERT INTO samples VALUES (?, ?)", (1000 + i, json.dumps(ex)))
        c.execute("INSERT INTO samples VALUES (?, ?)", (1050.5, None))
        c.execute("INSERT INTO samples VALUES (?, ?)", (1051.5, "not json"))
        c.execute("INSERT INTO samples VALUES (?, ?)", (900, json.dumps({"disks": {"2 C: D:": {"lat_ms": 999, "rd_mbs": 999}}})))
        for i, v in enumerate((10.0, 30.0, 20.0)):
            c.execute("INSERT INTO game_proc VALUES (?, 7, ?)", (1000 + i * 5, v))
        info = {"disk_map": [{"letter": "C", "disk": 2}, {"letter": "D", "disk": 2}, {"letter": "E", "disk": 1}],
                "disk_health": [hd(2), hd(1, "Goodram CX400 1TB", "SATA"), hd(0, "Goodram S25A 240GB", "SATA")]}
        c.execute("INSERT INTO system_info VALUES (?, ?)", (500, json.dumps(info)))

    def test_nvme(self):
        r = di.session_disk(self.c, 1000, 1099, r"D:\Games\x\game.exe", session=7)
        d = {p["disk"]: p for p in r["per_disk"]}
        self.assertEqual(set(d), {"0 F:", "1 E:", "2 C: D:"})
        self.assertEqual(d["2 C: D:"]["lat_max_ms"], 99.0)
        self.assertEqual(d["2 C: D:"]["lat_p95_ms"], 94.05)
        self.assertEqual(d["2 C: D:"]["lat_p99_ms"], 98.01)
        self.assertEqual(d["2 C: D:"]["read_mb"], 200.0)
        self.assertEqual((r["game_drive"], r["game_disk"]), ("D:", "2 C: D:"))
        self.assertEqual((r["game_disk_name"], r["game_disk_bus"]), ("Samsung SSD 990 PRO 2TB", "NVMe"))
        self.assertEqual(r["game_read"], {"avg_mbs": 20.0, "max_mbs": 30.0})

    def test_sata_and_no_session_arg(self):
        r = di.session_disk(self.c, 1000, 1099, "e:/Games/g.exe")
        self.assertEqual((r["game_disk"], r["game_disk_name"], r["game_disk_bus"]), ("1 E:", "Goodram CX400 1TB", "SATA"))
        self.assertEqual(r["game_read"]["max_mbs"], 30.0)

    def test_unknowns(self):
        r = di.session_disk(self.c, 1000, 1099, "Z:\\g.exe")
        self.assertEqual((r["game_disk"], r["game_disk_name"], r["game_disk_bus"]), (None, None, None))
        r = di.session_disk(self.c, 1000, 1099, None)
        self.assertIsNone(r["game_drive"])
        r = di.session_disk(self.c, 5000, 6000, "D:\\g.exe", session=99)    # nothing in range
        self.assertEqual(r["per_disk"], [])
        self.assertEqual(r["game_read"], {"avg_mbs": None, "max_mbs": None})

    def test_no_system_info_health(self):
        self.c.execute("DELETE FROM system_info")
        r = di.session_disk(self.c, 1000, 1099, "D:\\g.exe")
        self.assertEqual(r["game_disk"], "2 C: D:")
        self.assertIsNone(r["game_disk_name"])

    def test_disk_map_fallback_when_no_samples(self):
        r = di.session_disk(self.c, 5000, 6000, "E:\\g.exe")
        self.assertIsNone(r["game_disk"])
        self.assertEqual(r["game_disk_name"], "Goodram CX400 1TB")


class Findings(unittest.TestCase):
    def rep(self, cc, **disk):
        return {"stutters": {"cause_counts": cc}, "disk": disk}

    def test_slow_disk(self):
        f = di.findings(self.rep({"disk": 2, "disk_reset": 1}, game_drive="E:", game_disk="1 E:",
                                 game_disk_name="Goodram CX400 1TB", game_disk_bus="SATA", per_disk=[]))
        self.assertEqual([x["severity"] for x in f], ["warning", "serious"])
        self.assertIn("Gra na wolnym dysku (Goodram CX400 1TB, SATA), 3 stutterów dyskowych", f[0]["title"])
        self.assertIn("przenieś grę na Samsung 990 PRO (NVMe)", f[0]["detail"])
        self.assertIn("Samsung Magician", f[1]["detail"])

    def test_by_name_only(self):
        f = di.findings(self.rep({"disk": 3}, game_disk_name="Goodram CX400", game_disk_bus=None))
        self.assertEqual([x["severity"] for x in f], ["warning"])

    def test_nvme_no_slow_warning(self):
        self.assertEqual(di.findings(self.rep({"disk": 9}, game_disk_name="Samsung SSD 990 PRO", game_disk_bus="NVMe")), [])
        self.assertEqual(di.findings(self.rep({"disk": 2}, game_disk_bus="SATA")), [])     # below threshold
        self.assertEqual(di.findings({}), [])

    def test_reset_only(self):
        f = di.findings(self.rep({"disk_reset": 1}, game_disk_bus="NVMe"))
        self.assertEqual([x["severity"] for x in f], ["serious"])
        self.assertIn("NVMe", f[0]["title"])


class CliHelpers(unittest.TestCase):
    def test_rows_and_warnings(self):
        info = {"disk_health": [hd(2, temp=72, wear=81, read_unc=0, write_unc=2, power_on_h=1000),
                                hd(1, "Goodram CX400", "SATA", temp=33, wear=3, read_unc=0, write_unc=0)]}
        rows = di.health_rows(info)
        self.assertEqual(rows[0]["name"], "Samsung SSD 990 PRO 2TB")
        self.assertEqual(rows[0]["power_on_h"], 1000)
        self.assertEqual(set(rows[1]), {"name", "bus", "temp", "wear", "read_unc", "write_unc", "power_on_h",
                                         "read_lat_max", "write_lat_max"})
        w = di.health_warnings(info)
        self.assertEqual(len(w), 3)
        self.assertTrue(all("Goodram" not in x for x in w))
        self.assertEqual(di.health_warnings({}), [])
        self.assertEqual(di.health_rows({}), [])
        self.assertEqual(di.health_warnings({"disk_health": [hd(1, temp=None, wear=None, read_unc=None)]}), [])


if __name__ == "__main__":
    unittest.main()
