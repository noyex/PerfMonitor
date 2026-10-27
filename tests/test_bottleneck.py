import json
import sqlite3
import threading
import unittest

from perfmon import analyze as an
from perfmon import bottleneck, db

BASE = dict(bound_source="presentmon", gpu_bound_pct=0.0, cpu_bound_pct=0.0, limit_pct=0.0, gpu_power_limited_pct=None,
            game_thread_p95=None, vram_shared_max_mb=None, vram_shared_over_pct=None, limit_low_fps_pct=0.0)


def m(**kw):
    return dict(BASE, **kw)


def new_db():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    db.migrate(con)
    con.row_factory = sqlite3.Row
    return con


def add_session(con, sid, start, minutes=40, bound="GPU", fps=120.0, gpu_load=99.0, exe="game.exe", ctx=None):
    con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts, end_reason, summary, context) VALUES (?,?,?,?,?,?,?)",
                (sid, exe, start, start + minutes * 60, "exit", "{}", json.dumps(ctx or {})))
    for i in range(minutes * 60):
        ts = start + i
        con.execute("INSERT INTO frames(ts, session, frames, fps, ft_avg, cpu_busy, bound) VALUES (?,?,?,?,?,?,?)",
                    (ts, sid, int(fps), fps, 1000 / fps, 3.0, bound))
        con.execute("INSERT INTO samples(ts, gpu_load, gpu_power_w, gpu_clock_mhz, gpu_vram_total_mb) VALUES (?,?,?,?,?)",
                    (ts + 0.3, gpu_load, 300.0, 2900.0, 16384))


class ClassifyTest(unittest.TestCase):
    def test_order_and_kinds(self):
        self.assertEqual(bottleneck.classify(m(vram_shared_over_pct=15, vram_shared_max_mb=1500, gpu_bound_pct=90))[0],
                         "vram")
        self.assertEqual(bottleneck.classify(m(gpu_bound_pct=80, gpu_power_limited_pct=70))[0], "gpu_power")
        self.assertEqual(bottleneck.classify(m(gpu_bound_pct=10, gpu_power_limited_pct=70, cpu_bound_pct=50))[0], "cpu")
        self.assertEqual(bottleneck.classify(m(cpu_bound_pct=50, game_thread_p95=97))[0], "cpu_thread")
        self.assertEqual(bottleneck.classify(m(limit_pct=60, limit_low_fps_pct=25))[0], "limit")
        self.assertEqual(bottleneck.classify(m(limit_pct=60, limit_low_fps_pct=5))[0], "mixed")
        self.assertEqual(bottleneck.classify(m(gpu_bound_pct=65))[0], "gpu")


class ComputeTest(unittest.TestCase):
    def test_presentmon_bound(self):
        con = new_db()
        add_session(con, 1, 1000.0, minutes=5)
        b = bottleneck.compute(con, 1, 1000.0, 1300.0)
        self.assertEqual(b["kind"], "gpu")
        self.assertEqual(b["metrics"]["bound_source"], "presentmon")
        self.assertEqual(b["metrics"]["gpu_bound_pct"], 100.0)
        self.assertIn("->", bottleneck.describe(b))

    def test_gpu_load_fallback_without_gpubusy(self):
        con = new_db()
        add_session(con, 1, 1000.0, minutes=5, bound=None, gpu_load=60.0, fps=200.0)   # cpu_busy 3 of 5 ms
        b = bottleneck.compute(con, 1, 1000.0, 1300.0)
        self.assertEqual(b["metrics"]["bound_source"], "gpu_load")
        self.assertEqual(b["metrics"]["limit_pct"], 100.0)
        con2 = new_db()
        add_session(con2, 1, 1000.0, minutes=5, bound=None, gpu_load=99.0)
        self.assertEqual(bottleneck.compute(con2, 1, 1000.0, 1300.0)["kind"], "gpu")

    def test_no_frames(self):
        self.assertIsNone(bottleneck.compute(new_db(), 1, 0, 100))

    def test_collector_save_summary_merges(self):
        from perfmon.collector import Collector
        con = new_db()
        add_session(con, 1, 1000.0, minutes=2)
        con.execute("UPDATE sessions SET summary=? WHERE id=1", (json.dumps({"crash": {"module": "x.dll"}}),))

        class Cap:
            summary = staticmethod(lambda: {"frames": 10, "fps_avg": 120})
        c = object.__new__(Collector)
        c.con, c.lock = con, threading.Lock()
        c.save_summary({"session": 1, "capture": Cap(), "start": 1000.0})
        summ = json.loads(con.execute("SELECT summary FROM sessions WHERE id=1").fetchone()[0])
        self.assertEqual(summ["crash"], {"module": "x.dll"})
        self.assertEqual(summ["bottleneck"]["kind"], "gpu")
        self.assertIn("vram_shared_max_mb", summ)


class BeforeAfterTest(unittest.TestCase):
    def setUp(self):
        self.con = new_db()
        self.addCleanup(self.con.close)
        day = 86400
        for i in range(3):
            add_session(self.con, i + 1, 1_790_000_000 + i * day, fps=100.0, ctx={"adrenalin": "26.9.1"})
        for i in range(3):
            add_session(self.con, i + 4, 1_790_000_000 + (i + 5) * day, fps=120.0, ctx={"adrenalin": "26.10.1"})
        self.split = 1_790_000_000 + 4 * day

    def test_split_by_time(self):
        rows = an.compare(self.con, "game.exe", before=self.split)
        self.assertEqual([r["group"] for r in rows], ["przed", "po"])
        self.assertEqual([r["sessions"] for r in rows], [3, 3])
        self.assertAlmostEqual(rows[1]["fps_avg_delta_pct"], 20.0)
        self.assertFalse(any(r["small_sample"] for r in rows))
        self.assertEqual(rows[1]["bottleneck"], {"gpu": 100.0})

    def test_after_skips_sessions_in_between(self):
        rows = an.compare(self.con, "game.exe", before=self.split, after=self.split + 2 * 86400)
        self.assertEqual([r["sessions"] for r in rows], [3, 2])
        self.assertTrue(all(r["small_sample"] for r in rows))

    def test_marker_split(self):
        ms = an.timeline_markers(self.con, "game.exe")
        self.assertEqual([(x["n"], x["kind"], x["session"]) for x in ms], [(1, "driver", 4)])
        self.assertEqual(an.resolve_point(self.con, "m1", "game.exe"), ms[0]["ts"])
        self.assertEqual(an.resolve_point(self.con, "#1", "game.exe"), ms[0]["ts"])
        rows = an.compare(self.con, "game.exe", before="m1")
        self.assertEqual([r["sessions"] for r in rows], [3, 3])
        with self.assertRaises(ValueError):
            an.resolve_point(self.con, "m9", "game.exe")

    def test_marker_numbers_stable_with_since(self):
        self.assertEqual(an.timeline_markers(self.con, "game.exe", since=self.split)[0]["n"], 1)
        self.assertEqual(an.timeline_markers(self.con, "game.exe", since=self.split + 10 * 86400), [])


if __name__ == "__main__":
    unittest.main()
