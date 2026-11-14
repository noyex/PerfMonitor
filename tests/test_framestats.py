import sqlite3
import unittest

from perfmon import db, framestats as fs


class FrameStatsTest(unittest.TestCase):
    def test_bound_against_application_frametime(self):
        self.assertEqual(fs.bound(10.0, 5.0, 9.0), "GPU")
        self.assertEqual(fs.bound(10.0, 9.0, 5.0), "CPU")
        self.assertEqual(fs.bound(10.0, 5.0, 5.0), "limit")
        self.assertIsNone(fs.bound(10.0, None, 5.0))
        self.assertIsNone(fs.bound(None, 5.0, 5.0))

    def test_stutter_side(self):
        self.assertEqual(fs.stutter_side(50, 40, 8), "cpu")
        self.assertEqual(fs.stutter_side(50, 8, 30), "gpu")
        self.assertEqual(fs.stutter_side(50, 8, 8), "present")
        self.assertEqual(fs.stutter_side(50, None, 30), "gpu")
        self.assertIsNone(fs.stutter_side(50, None, None))

    def test_pacing(self):
        self.assertIsNone(fs.pacing(0, 0, 0, 0, 0))
        p = fs.pacing(10, 20, 100, 1000, 100)
        self.assertEqual(p, {"anim_err_ms": 0.5, "disp_jitter_ms": 1.0, "disp_jitter_pct": 10.0})

    def test_pacing_rows_skip_old_rows(self):
        rows = [{"frames": 100, "fps": 100, "anim_err": 1.0, "disp_ft_sd": 2.0},
                {"frames": 100, "fps": 100, "anim_err": None, "disp_ft_sd": None}]
        self.assertEqual(fs.pacing_rows(rows), {"anim_err_ms": 1.0, "disp_jitter_ms": 2.0, "disp_jitter_pct": 20.0})
        self.assertIsNone(fs.pacing_rows(rows[1:]))

    def test_app_sql_falls_back_on_old_rows(self):
        con = sqlite3.connect(":memory:")
        db.migrate(con)
        con.executemany("INSERT INTO frames(ts, session, frames, fps, ft_p99, app_frames, app_fps, app_ft_p99) "
                        "VALUES (?,1,?,?,?,?,?,?)", [(1, 200, 200, 6, 100, 100, 12),     # frame generation
                                                    (2, 100, 100, 11, None, None, None),  # recorded before
                                                    (3, 4, 200, 6, 0, None, None)])       # only generated frames
        got = con.execute(f"SELECT {fs.APP_FPS}, {fs.APP_FRAMES}, {fs.APP_FT_P99}, {fs.APP_FT_AVG} FROM frames "
                          "ORDER BY ts").fetchall()
        self.assertEqual(got[0], (100, 100, 12, 10.0))
        self.assertEqual(got[1][:3], (100, 100, 11))
        self.assertEqual(got[2][:2], (None, 0))

    def test_side_ranking(self):
        st = [{"side": "cpu"}] * 6 + [{"side": "gpu"}] * 2 + [{"side": None}]
        bound_at = {t: "GPU" for t in range(100)}
        r = {x["tag"]: x for x in fs.side_ranking(st, range(100), bound_at)}
        self.assertEqual(r["cpu"]["verdict"], "confirmed")   # never CPU-bound in normal play
        self.assertEqual(r["gpu"]["verdict"], "ambient")     # GPU is the normal state of this game
        self.assertIsNone(fs.side_ranking([{"side": None}], range(10), bound_at))


if __name__ == "__main__":
    unittest.main()
