import unittest

from perfmon import analyze as an
from perfmon import episodes
from tests.test_bottleneck import new_db


def row(i, ts, kind="commit_full", end=None, **det):
    return {"id": i, "ts": ts, "end_ts": end if end is not None else ts + 1, "kind": kind, "severity": "serious",
            "title": kind, "details": det or None}


class TrackerTest(unittest.TestCase):
    def test_flapping_is_one_episode(self):
        t = episodes.Tracker()
        t.start("commit_full", {"ts": 0, "commit_gb": 37.0, "commit_limit_gb": 40.0})
        self.assertFalse(t.off("commit_full", 3))
        t.hit("commit_full", {"ts": 10, "commit_gb": 39.0, "commit_limit_gb": 40.0})   # back within the gap
        t.hit("commit_full", {"ts": 11, "commit_gb": 38.0, "commit_limit_gb": 40.0})
        self.assertFalse(t.off("commit_full", 12))
        self.assertFalse(t.off("commit_full", 100))
        self.assertTrue(t.off("commit_full", 11 + episodes.GAP_S["commit_full"]))
        end, patch = t.close("commit_full")
        self.assertEqual(end, 12)   # first second off, not when the gap ran out
        self.assertEqual(patch, {"count": 2, "worst": {"commit_pct": 97.5}})

    def test_first_off_second_and_return_are_reported(self):
        t = episodes.Tracker()
        t.start("dwm_stall", {"ts": 0, "dwm_ms": 2500})
        self.assertFalse(t.off("dwm_stall", 5))
        self.assertTrue(t.just_off("dwm_stall", 5))   # collector writes a provisional end here
        t.off("dwm_stall", 6)
        self.assertFalse(t.just_off("dwm_stall", 6))
        self.assertTrue(t.hit("dwm_stall", {"ts": 7, "dwm_ms": 2600}))   # back: provisional end cleared
        self.assertFalse(t.hit("dwm_stall", {"ts": 8, "dwm_ms": 2600}))

    def test_kind_without_gap_closes_at_once(self):
        t = episodes.Tracker()
        t.start("unknown_kind", {"ts": 0})
        self.assertTrue(t.off("unknown_kind", 1))
        self.assertEqual(t.close("unknown_kind"), (1, {"count": 1}))

    def test_hot_kinds_track_their_sensor(self):
        t = episodes.Tracker()
        t.start("hot_cpu_temp", {"ts": 0, "cpu_temp": 96.0})
        t.hit("hot_cpu_temp", {"ts": 1, "cpu_temp": 98.0})
        self.assertEqual(t.close("hot_cpu_temp", 5)[1]["worst"], {"value": 98.0})

    def test_dwm_visible(self):
        self.assertTrue(episodes.dwm_visible(2.0))
        self.assertTrue(episodes.dwm_visible(None))
        self.assertFalse(episodes.dwm_visible(600.0))
        self.assertTrue(episodes.dwm_visible(600.0, game_session=5))   # gamepad input is invisible to Windows


class GroupTest(unittest.TestCase):
    def test_merges_within_gap_only(self):
        rows = [row(1, 0, end=1), row(2, 3, end=4), row(3, 60, end=80), row(4, 1000, end=1001)]
        g = episodes.group(rows)
        self.assertEqual([(x["id"], x["count"], x["ts"], x["end_ts"]) for x in g], [(1, 3, 0, 80), (4, 1, 1000, 1001)])

    def test_latest_row_decides_whether_the_episode_is_open(self):
        dead = dict(row(1, 0), end_ts=None)   # left open by a collector that died
        g = episodes.group([dead, row(2, 5, end=9)])
        self.assertEqual((g[0]["count"], g[0]["end_ts"]), (2, 9))
        g = episodes.group([row(1, 0, end=1), dict(row(2, 5), end_ts=None)])
        self.assertIsNone(g[0]["end_ts"])

    def test_other_kinds_pass_through(self):
        rows = [row(1, 0, kind="app_crash"), row(2, 1, kind="app_crash")]
        self.assertEqual([x["count"] for x in episodes.group(rows)], [1, 1])

    def test_interleaved_kinds_keep_their_episode(self):
        rows = [row(1, 0, kind="dwm_stall", user_idle_s=1), row(2, 5, kind="commit_full"),
                row(3, 20, kind="dwm_stall", user_idle_s=2)]
        self.assertEqual([(x["kind"], x["count"]) for x in episodes.group(rows)], [("dwm_stall", 2), ("commit_full", 1)])

    def test_worst_and_collector_count(self):
        rows = [row(1, 0, kind="dwm_stall", end=19, dwm_ms=2200, user_idle_s=0),
                row(2, 20, kind="dwm_stall", end=39, dwm_ms=2900, user_idle_s=20),
                row(3, 300, kind="dwm_stall", count=4, worst={"dwm_ms": 3100}, user_idle_s=0)]
        g = episodes.group(rows)
        self.assertEqual([(x["count"], x["worst"]) for x in g], [(2, {"dwm_ms": 2900}), (4, {"dwm_ms": 3100})])

    def test_idle_dwm_stalls_hidden_unless_in_game(self):
        con = new_db()
        con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts) VALUES (1, 'game.exe', 5000, 6000)")
        rows = [row(1, 100, kind="dwm_stall", user_idle_s=900), row(2, 5500, kind="dwm_stall", user_idle_s=900),
                row(3, 7000, kind="dwm_stall", user_idle_s=3), row(4, 8000, kind="dwm_stall", user_idle_s=900, count=1)]
        self.assertEqual([x["id"] for x in episodes.group(rows, con)], [2, 3, 4])


class IncidentsApiTest(unittest.TestCase):
    def test_grouped_listing_and_health_counts(self):
        con = new_db()
        for i in range(26):   # one low-memory episode written as 26 rows by the old collector
            con.execute("INSERT INTO incidents(ts, end_ts, kind, severity, title) VALUES (?,?,?,?,?)",
                        (1e9 + 10 * i, 1e9 + 10 * i + 2, "commit_full", "serious", "commit"))
        con.execute("INSERT INTO incidents(ts, kind, severity, title, details) VALUES (?,?,?,?,?)",
                    (1e9 + 5000, "app_crash", "serious", "Crash: x.exe", '{"a": 1}'))
        rows = an.incidents(con, 1e9 - 1)
        self.assertEqual([(r["kind"], r["count"]) for r in rows], [("commit_full", 26), ("app_crash", 1)])
        self.assertEqual(an.incident(con, rows[1]["id"])["details"], {"a": 1})
        raw = an.incidents_between(con, 1e9 - 1, 2e9)
        self.assertEqual(len(raw), 27)


if __name__ == "__main__":
    unittest.main()
