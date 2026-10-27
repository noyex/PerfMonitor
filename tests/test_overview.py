import json
import time
import unittest

from perfmon import analyze as an
from tests.test_bottleneck import add_session, new_db


def add_open_session(con, sid, start, exe="live.exe"):
    con.execute("INSERT INTO sessions(id, exe, start_ts, summary) VALUES (?,?,?,?)",
                (sid, exe, start, json.dumps({"fps_avg": 140.0, "fps_1low": 98.0})))
    for i in range(120):
        ts = start + i
        con.execute("INSERT INTO frames(ts, session, frames, fps, ft_avg, cpu_busy, bound) VALUES (?,?,?,?,?,?,?)",
                    (ts, sid, 140, 140.0, 7.1, 3.0, "GPU"))


class OverviewTest(unittest.TestCase):
    def test_running_session_is_current_with_live_bottleneck_and_stutters(self):
        con = new_db()
        now = time.time()
        add_session(con, 1, now - 86400, minutes=2)
        con.execute("UPDATE sessions SET summary=? WHERE id=1", (json.dumps({"frames": 7200}),))
        add_open_session(con, 2, now - 120)
        con.executemany("INSERT INTO stutters(ts, session, ft_ms, median_ms, causes) VALUES (?,?,?,?,?)",
                        [(now - 90, 2, 40, 7, "[]"), (now - 30, 2, 50, 7, "[]")])
        o = an.overview(con)
        cur = o["current"]
        self.assertEqual(cur["id"], 2)
        self.assertTrue(cur["running"])
        self.assertEqual(cur["stutters"], 2)
        self.assertAlmostEqual(cur["last_stutter_ts"], now - 30)
        self.assertEqual(len(cur["stutter_ts"]), 2)
        self.assertEqual(cur["bottleneck"]["kind"], "gpu")
        self.assertEqual(cur["bottleneck"]["share"], 100.0)
        self.assertEqual([r["id"] for r in o["recent"]], [1])

    def test_idle_shows_last_session_with_frames_and_skips_launchers(self):
        con = new_db()
        now = time.time()
        add_session(con, 1, now - 7200, minutes=2)
        # launcher: ended normally without frames -> neither current nor recent
        con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts, end_reason, summary) VALUES (3, 'launcher.exe', ?, ?, 'exit', ?)",
                    (now - 600, now - 550, json.dumps({"frames": 0})))
        # crash without frames stays in the list
        con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts, end_reason, summary) VALUES (2, 'crashy.exe', ?, ?, 'crash', '{}')",
                    (now - 3600, now - 3500))
        con.execute("UPDATE sessions SET summary=? WHERE id=1", (json.dumps({"frames": 7200}),))
        o = an.overview(con)
        self.assertEqual(o["current"]["id"], 1)
        self.assertFalse(o["current"]["running"])
        self.assertIsNotNone(o["current"]["bottleneck"])
        self.assertEqual([r["id"] for r in o["recent"]], [2])

    def test_empty_db(self):
        o = an.overview(new_db())
        self.assertIsNone(o["current"])
        self.assertEqual(o["recent"], [])
        self.assertFalse(o["collector_alive"])


if __name__ == "__main__":
    unittest.main()
