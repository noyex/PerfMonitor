import sqlite3
import unittest

from perfmon import diagreport, gpuproc, config
from perfmon import db


def inst(pid, eng, typ="3D"):
    return f"pid_{pid}_luid_0x0_0x16A87_phys_0_eng_{eng}_engtype_{typ}"


class AggregateTest(unittest.TestCase):
    def test_sums_engines_and_caps_at_100(self):
        out = gpuproc.aggregate({inst(1, 0): 60.0, inst(1, 1, "Copy"): 70.0, inst(2, 0): 12.5}, {}, {})
        self.assertEqual(out[1]["gpu"], 100.0)
        self.assertEqual(out[1]["gpu_3d"], 60.0)
        self.assertEqual(out[2]["gpu"], 12.5)

    def test_garbage_engine_value_is_dropped(self):
        # PDH returned 3.07e13 % for AMDRSServ.exe (fresh instance / reset counter)
        out = gpuproc.aggregate({inst(9, 0): 3.07e13, inst(9, 1, "Copy"): 2.0, inst(3, 0): float("nan"),
                                 inst(4, 0): -5.0}, {}, {})
        self.assertEqual(out[9]["gpu"], 2.0)
        self.assertNotIn(3, out)
        self.assertNotIn(4, out)
        self.assertEqual(gpuproc.top_background(out, set(), min_pct=1.0)[0][0], 9)


class HistoricalRowsTest(unittest.TestCase):
    def test_report_ignores_bad_rows(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        rows = [(100.0 + i * 5, "gpu", "AMDRSServ.exe", 7, None, None, None, 3.07e13 if i % 2 else 3.0, 100.0)
                for i in range(10)]
        rows += [(100.0 + i * 5, "gpu", "chrome.exe", 8, None, None, None, 20.0, 50.0) for i in range(10)]
        con.executemany("INSERT INTO procs(ts, kind, name, pid, cpu_pct, mem_mb, io_mbs, gpu_pct, vram_mb) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        bg = {r["name"]: r for r in diagreport.gpu_background(con, 90, 200, "game.exe")}
        self.assertLessEqual(bg["AMDRSServ.exe"]["max_pct"], config.GPU_PCT_MAX)
        self.assertLess(bg["AMDRSServ.exe"]["avg_pct"], 5)
        self.assertEqual(bg["chrome.exe"]["avg_pct"], 20.0)
        rep = diagreport.StutterExt(con, {"exe": "game.exe", "id": 1}, 90, 200)
        self.assertEqual(rep.background_gpu(130.0), ["chrome.exe"])


if __name__ == "__main__":
    unittest.main()
