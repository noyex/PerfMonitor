import json
import sqlite3
import threading
import unittest

from perfmon import db, memleak

START = 100000.0


def make_db(minutes, mb_at, fps_at=lambda t: 120.0):
    con = sqlite3.connect(":memory:", check_same_thread=False)
    db.migrate(con)
    con.row_factory = sqlite3.Row
    end = START + minutes * 60
    con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts, summary) VALUES (1, 'game.exe', ?, ?, '{}')",
                (START, end))
    t = START
    while t < end:
        con.execute("INSERT INTO game_proc(ts, session, pid, private_mb) VALUES (?,?,?,?)", (t, 1, 5, mb_at(t - START)))
        t += 5
    for s in range(int(START), int(end)):
        fps = fps_at(s - START)
        con.execute("INSERT INTO frames(ts, session, frames, fps) VALUES (?,?,?,?)", (s, 1, int(fps), fps))
    return con, end


class MemLeakTest(unittest.TestCase):
    def test_fit(self):
        slope, r2 = memleak.fit([(t, 2 * t + 1) for t in range(20)])
        self.assertAlmostEqual(slope, 2)
        self.assertAlmostEqual(r2, 1)
        self.assertIsNone(memleak.fit([(1, 1)] * 20))

    def test_steady_leak(self):
        con, end = make_db(60, lambda t: 4000 + t * 1000 / 3600 + (t % 30) * 0.5)   # +1 GB/h
        m = memleak.assess(con, 1, START, end)
        self.assertTrue(m["leak"])
        self.assertAlmostEqual(m["mem_growth_mb_h"], 1000, delta=20)
        self.assertGreater(m["mem_growth_r2"], 0.95)
        self.assertEqual(memleak.finding("game.exe", m)[0]["severity"], "warning")

    def test_flat_memory_no_leak(self):
        con, end = make_db(60, lambda t: 6000 + (t % 50))
        m = memleak.assess(con, 1, START, end)
        self.assertFalse(m["leak"])
        self.assertEqual(memleak.finding("game.exe", m), [])

    def test_slow_monotonic_growth_with_fps_drop(self):
        con, end = make_db(60, lambda t: 5000 + t * 200 / 3600, fps_at=lambda t: 140 if t < 2700 else 100)
        m = memleak.assess(con, 1, START, end)
        self.assertTrue(m["monotonic"])
        self.assertLess(m["mem_growth_mb_h"], memleak.LEAK_MB_H)
        self.assertTrue(m["leak"])

    def test_short_session_skipped(self):
        con, end = make_db(20, lambda t: 4000 + t)
        self.assertIsNone(memleak.assess(con, 1, START, end))

    def test_collector_saves_summary_and_incident(self):
        from perfmon.collector import Collector
        con, end = make_db(60, lambda t: 4000 + t * 1000 / 3600)
        c = object.__new__(Collector)
        c.con, c.lock = con, threading.Lock()
        c.save_mem_growth(1, "game.exe", end)
        summ = json.loads(con.execute("SELECT summary FROM sessions WHERE id=1").fetchone()[0])
        self.assertAlmostEqual(summ["mem_growth_mb_h"], 1000, delta=5)
        inc = con.execute("SELECT kind, severity, title FROM incidents").fetchall()
        self.assertEqual([tuple(i)[:2] for i in inc], [("game_mem_leak", "warning")])


if __name__ == "__main__":
    unittest.main()
