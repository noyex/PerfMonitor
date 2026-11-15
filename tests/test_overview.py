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


def add_finished(con, sid, start, seconds, frames=None, fps=120.0, reason="exit"):
    summ = {"frames": frames, "fps_avg": fps if frames else None}
    con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts, end_reason, summary) VALUES (?,?,?,?,?,?)",
                (sid, "game.exe", start, start + seconds, reason, json.dumps(summ)))


class OverviewTest(unittest.TestCase):
    def test_running_session_is_current_with_live_stats(self):
        con = new_db()
        now = time.time()
        add_session(con, 1, now - 86400, minutes=2)
        con.execute("UPDATE sessions SET summary=? WHERE id=1", (json.dumps({"frames": 7200}),))
        add_open_session(con, 2, now - 120)
        con.executemany("INSERT INTO stutters(ts, session, ft_ms, median_ms, causes) VALUES (?,?,?,?,?)",
                        [(now - 90, 2, 40, 7, "[]"), (now - 30, 2, 50, 7, "[]")])
        con.execute("INSERT INTO samples(ts) VALUES (?)", (now - 1,))   # collector alive
        o = an.overview(con)
        cur = o["current"]
        self.assertEqual(cur["id"], 2)
        self.assertTrue(cur["running"])
        self.assertEqual(cur["stutters"], 2)
        self.assertEqual(cur["fps_live"], 140.0)
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
        self.assertEqual([r["id"] for r in o["recent"]], [2])

    def test_stale_open_session_falls_back_to_last_finished(self):
        con = new_db()
        now = time.time()
        add_session(con, 1, now - 7200, minutes=2)
        con.execute("UPDATE sessions SET summary=? WHERE id=1", (json.dumps({"frames": 7200}),))
        add_open_session(con, 2, now - 3600)          # frames an hour old, no samples: collector is dead
        o = an.overview(con)
        self.assertEqual(o["current"]["id"], 1)
        self.assertEqual(o["recent"], [])

    def test_empty_db(self):
        o = an.overview(new_db())
        self.assertIsNone(o["current"])
        self.assertEqual(o["recent"], [])
        self.assertIsNone(o["temps"])

    def test_running_session_longest_frame_comes_from_frames(self):
        con = new_db()
        now = time.time()
        add_open_session(con, 1, now - 120)
        con.execute("UPDATE frames SET ft_max=412.5 WHERE session=1 AND ts=(SELECT MAX(ts) FROM frames)")
        con.execute("INSERT INTO samples(ts) VALUES (?)", (now - 1,))
        self.assertEqual(an.overview(con)["current"]["ft_max"], 412.5)

    def test_live_fps_covers_only_the_last_two_minutes(self):
        con = new_db()
        now = time.time()
        add_open_session(con, 1, now - 3000)                                 # 140 fps, long ago
        con.execute("INSERT INTO frames(ts, session, frames, fps) VALUES (?,1,60,60.0)", (now - 10,))
        con.execute("INSERT INTO samples(ts) VALUES (?)", (now - 1,))
        self.assertEqual(an.overview(con)["current"]["fps_live"], 60.0)

    def test_running_session_counts_and_stale_open_row_does_not(self):
        con = new_db()
        now = time.time()
        add_open_session(con, 1, now - 30 * 86400)                           # never closed
        add_open_session(con, 2, now - 120)
        con.execute("INSERT INTO samples(ts) VALUES (?)", (now - 1,))
        o = an.overview(con)
        self.assertEqual(o["current"]["id"], 2)

    def test_game_running_covers_a_game_without_recent_frames(self):
        con = new_db()
        now = time.time()
        add_open_session(con, 1, now - 3600)                                 # frames an hour old: not `current`
        self.assertFalse(an.overview(con)["game_running"])                   # no collector
        con.execute("INSERT INTO samples(ts) VALUES (?)", (now - 1,))
        o = an.overview(con)
        self.assertIsNone(o["current"])
        self.assertTrue(o["game_running"])

    def test_temps_are_live_only(self):
        con = new_db()
        now = time.time()
        con.execute("INSERT INTO samples(ts, cpu_temp, gpu_temp) VALUES (?,?,?)", (now - 1, 61.04, 58.0))
        self.assertEqual(an.overview(con)["temps"], {"cpu": 61.0, "gpu": 58.0})
        con.execute("UPDATE samples SET ts=?", (now - 60,))
        self.assertIsNone(an.overview(con)["temps"])


if __name__ == "__main__":
    unittest.main()
