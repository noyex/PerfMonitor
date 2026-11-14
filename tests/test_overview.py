import json
import time
import unittest
from datetime import datetime, timedelta

from perfmon import analyze as an
from tests.test_bottleneck import add_session, new_db


def add_open_session(con, sid, start, exe="live.exe"):
    con.execute("INSERT INTO sessions(id, exe, start_ts, summary) VALUES (?,?,?,?)",
                (sid, exe, start, json.dumps({"fps_avg": 140.0, "fps_1low": 98.0})))
    for i in range(120):
        ts = start + i
        con.execute("INSERT INTO frames(ts, session, frames, fps, ft_avg, cpu_busy, bound) VALUES (?,?,?,?,?,?,?)",
                    (ts, sid, 140, 140.0, 7.1, 3.0, "GPU"))


def add_finished(con, sid, start, seconds, frames=None, fps=120.0, reason="exit", bottleneck=None):
    summ = {"frames": frames, "fps_avg": fps if frames else None, "bottleneck": bottleneck}
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
        self.assertEqual(cur["bottleneck"]["kind"], "gpu")
        self.assertEqual(cur["bottleneck"]["share"], 100.0)
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
        self.assertIsNotNone(o["current"]["bottleneck"])
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
        self.assertEqual(o["totals"], {"play_s": 0, "week_s": 0, "fps_avg": None, "crashes_14d": 0})
        self.assertIsNone(o["temps"])

    def test_finished_session_reads_the_flat_stored_bottleneck(self):
        con = new_db()
        now = time.time()
        add_finished(con, 1, now - 7200, 600, frames=1000,
                     bottleneck={"kind": "cpu", "why": "CPU-bound 50% czasu", "cpu_bound_pct": 50.0})
        add_finished(con, 2, now - 3600, 600, frames=1000,
                     bottleneck={"kind": "gpu", "why": "GPU-bound 89% czasu", "gpu_bound_pct": 89.0})
        o = an.overview(con)
        self.assertEqual((o["current"]["bottleneck"]["kind"], o["current"]["bottleneck"]["share"]), ("gpu", 89.0))
        self.assertEqual((o["recent"][0]["bottleneck"]["kind"], o["recent"][0]["bottleneck"]["share"]), ("cpu", 50.0))

    def test_session_without_frames_has_no_bottleneck(self):
        con = new_db()
        now = time.time()
        add_finished(con, 1, now - 7200, 100, reason="crash")
        add_finished(con, 2, now - 3600, 600, frames=1000)
        self.assertIsNone(an.overview(con)["recent"][0]["bottleneck"])

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

    def test_totals_weigh_fps_by_time_over_sessions_with_frames(self):
        con = new_db()
        now = time.time()
        add_finished(con, 1, now - 90 * 86400, 3000, frames=1000, fps=100.0)
        add_finished(con, 2, now - 60 * 86400, 1000, frames=1000, fps=200.0)
        add_finished(con, 3, now - 30 * 86400, 500, frames=0)                # PresentMon failed: time, no FPS
        t = an.overview(con)["totals"]
        self.assertEqual(t["fps_avg"], 125.0)
        self.assertEqual(t["play_s"], 4500)

    def test_broken_end_ts_uses_the_last_frame(self):
        con = new_db()
        now = time.time()
        add_finished(con, 1, now - 30 * 86400, 0, frames=1000, fps=100.0)    # hard reset: end_ts == start_ts
        con.execute("INSERT INTO frames(ts, session, fps) VALUES (?,1,100.0)", (now - 30 * 86400 + 900,))
        t = an.overview(con)["totals"]
        self.assertEqual(t["play_s"], 900)
        self.assertEqual(t["fps_avg"], 100.0)

    def test_week_starts_on_monday_midnight_local(self):
        con = new_db()
        now = time.time()
        today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        monday = (today - timedelta(days=today.weekday())).timestamp()
        add_finished(con, 1, monday - 7200, 600, frames=1000)                # last week
        add_finished(con, 2, monday - 60, 600, frames=1000)                  # over the edge: 540 s in this week
        add_finished(con, 3, monday + 1, 300, frames=1000)
        t = an.overview(con)["totals"]
        self.assertEqual(t["week_s"], 840)
        self.assertEqual(t["play_s"], 1500)

    def test_running_session_counts_and_stale_open_row_does_not(self):
        con = new_db()
        now = time.time()
        add_open_session(con, 1, now - 30 * 86400)                           # never closed
        add_open_session(con, 2, now - 120)
        con.execute("INSERT INTO samples(ts) VALUES (?)", (now - 1,))
        o = an.overview(con)
        self.assertEqual(o["current"]["id"], 2)
        self.assertAlmostEqual(o["totals"]["play_s"], 120, delta=2)

    def test_crashes_are_app_crash_incidents_of_the_last_14_days(self):
        con = new_db()
        now = time.time()
        con.executemany("INSERT INTO incidents(ts, kind, severity, title) VALUES (?,?,?,?)",
                        [(now - 86400, "app_crash", "serious", "a"), (now - 13 * 86400, "app_crash", "serious", "b"),
                         (now - 15 * 86400, "app_crash", "serious", "old"), (now - 3600, "app_hang", "serious", "hang")])
        self.assertEqual(an.overview(con)["totals"]["crashes_14d"], 2)

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


class FrameSeriesTest(unittest.TestCase):
    def test_missing_from_uses_the_session_or_the_last_hour(self):
        con = new_db()
        now = time.time()
        add_open_session(con, 2, now - 7200)   # 120 frames in its first two minutes, two hours ago
        con.execute("UPDATE sessions SET end_ts=? WHERE id=2", (now - 7080,))
        whole = an.frame_series(con, None, None, session=2, max_points=120)
        self.assertEqual(whole["step_s"], 1.0)   # spread over the session, not up to now
        self.assertEqual(len(whole["ts"]), 120)
        self.assertEqual(whole["ts"][0], now - 7200)
        self.assertEqual(an.frame_series(con, None, None)["ts"], [])   # last hour: nothing


if __name__ == "__main__":
    unittest.main()
