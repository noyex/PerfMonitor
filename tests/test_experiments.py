import json
import sqlite3
import unittest

from perfmon import db
from perfmon import experiments as ex

T0 = 1_700_000_000.0
DAY = 86400


def new_db():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    db.migrate(con)
    con.row_factory = sqlite3.Row
    return con


def add(con, start, fps=100.0, low=60.0, st=20, minutes=20, exe="g.exe", driver="24.1", ver="1.0", usable=True):
    gp = {"seconds": minutes * 60, "fps_avg": fps, "fps_1low": low, "stutters": st, "usable": usable}
    con.execute("INSERT INTO sessions(exe, start_ts, end_ts, end_reason, summary, context) VALUES (?,?,?,?,?,?)",
                (exe, start, start + minutes * 60, "exit", json.dumps({"gameplay": gp}),
                 json.dumps({"adrenalin": driver, "game_version": ver})))


def setup(before, after, **kw):
    """before/after: lists of dicts for add(); the test starts at T0."""
    con = new_db()
    for i, b in enumerate(before):
        add(con, T0 - (len(before) - i) * DAY, **{**kw, **b})
    tid = ex.start(con, "Instant Replay off", exe="g.exe", key="replay", now=T0)
    for i, a in enumerate(after):
        add(con, T0 + (i + 1) * DAY, **{**kw, **a})
    return con, tid


class VerdictTest(unittest.TestCase):
    def verdict(self, before, after, **kw):
        con, tid = setup(before, after, **kw)
        return ex.show(con, tid)

    def test_helped(self):
        r = self.verdict([{"fps": 100}] * 3, [{"fps": 112}] * 3)
        self.assertEqual(r["verdict"], "pomogło")
        self.assertTrue(r["metrics"][0]["significant"])

    def test_worse(self):
        self.assertEqual(self.verdict([{"fps": 100}] * 3, [{"fps": 88}] * 3)["verdict"], "pogorszyło")

    def test_mixed(self):
        r = self.verdict([{"fps": 100, "low": 60}] * 3, [{"fps": 112, "low": 50}] * 3)
        self.assertEqual(r["verdict"], "mieszane")

    def test_no_change(self):
        self.assertEqual(self.verdict([{"fps": 100}] * 3, [{"fps": 101}] * 3)["verdict"], "bez zmian")

    def test_stutters_lower_is_better(self):
        r = self.verdict([{"st": 40}] * 3, [{"st": 10}] * 3)
        self.assertEqual(r["verdict"], "pomogło")
        self.assertIs([m for m in r["metrics"] if m["key"] == "stutters_per_min"][0]["better"], True)

    def test_noise_blocks_significance(self):
        noisy_b = [{"fps": f} for f in (60, 140, 70, 130)]
        noisy_a = [{"fps": f} for f in (75, 150, 80, 140)]   # +9 fps on average but spread of ~40 fps per session
        r = self.verdict(noisy_b, noisy_a)
        self.assertEqual(r["verdict"], "bez zmian")
        self.assertGreater(r["metrics"][0]["noise"], 9)

    def test_too_little_data(self):
        r = self.verdict([{"fps": 100}] * 3, [{"fps": 112, "minutes": 12}])
        self.assertEqual(r["verdict"], "za mało danych")
        self.assertIn("brakuje 18 min gry po zmianie", r["missing"])

    def test_one_session_is_not_enough(self):
        r = self.verdict([{"fps": 100}] * 3, [{"fps": 112, "minutes": 45}])
        self.assertEqual(r["verdict"], "za mało danych")
        self.assertTrue(any("sesji po zmianie" in m for m in r["missing"]))

    def test_unusable_and_running_sessions_ignored(self):
        con, tid = setup([{"fps": 100}] * 3, [{"fps": 112}] * 3)
        add(con, T0 + 10 * DAY, fps=10, usable=False)
        con.execute("INSERT INTO sessions(exe, start_ts, end_ts, summary) VALUES ('g.exe', ?, NULL, '{}')", (T0 + 11 * DAY,))
        self.assertEqual(ex.show(con, tid)["after"]["sessions"], 3)

    def test_driver_warning(self):
        con, tid = setup([{"fps": 100}] * 3, [])
        for i in range(3):
            add(con, T0 + (i + 1) * DAY, fps=112, driver="24.2")
        r = ex.show(con, tid)
        self.assertTrue(any("sterownik" in w for w in r["warnings"]))

    def test_before_only_last_eight(self):
        con, tid = setup([{"fps": 50}] * 4 + [{"fps": 100}] * 8, [{"fps": 100}] * 3)
        self.assertEqual(ex.show(con, tid)["before"]["sessions"], 8)
        self.assertEqual(ex.show(con, tid)["verdict"], "bez zmian")


class LifecycleTest(unittest.TestCase):
    def test_single_open_test(self):
        con, _ = setup([{}] * 3, [])
        with self.assertRaises(ValueError) as c:
            ex.start(con, "druga zmiana", exe="g.exe")
        self.assertIn("Trwa już test", str(c.exception))

    def test_stop_stores_result_and_allows_next(self):
        con, tid = setup([{"fps": 100}] * 3, [{"fps": 112}] * 3)
        res = ex.stop(con)
        self.assertEqual(res["verdict"], "pomogło")
        row = con.execute("SELECT status, result FROM experiments WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(json.loads(row["result"])["verdict"], "pomogło")
        self.assertEqual(ex.by_key(con)["replay"]["verdict"], "pomogło")
        self.assertEqual(ex.list_all(con)[0]["verdict"], "pomogło")
        ex.start(con, "kolejny", exe="g.exe")

    def test_cancel(self):
        con, tid = setup([{}] * 3, [])
        ex.cancel(con)
        self.assertEqual(ex.list_all(con)[0]["status"], "cancelled")
        self.assertEqual(ex.by_key(con), {})
        with self.assertRaises(ValueError):
            ex.stop(con)

    def test_default_exe_most_gameplay(self):
        import time
        con = new_db()
        with self.assertRaises(ValueError):
            ex.start(con, "x")
        now = time.time()
        add(con, now - DAY, exe="a.exe", minutes=30)
        add(con, now - 2 * DAY, exe="b.exe", minutes=90)
        add(con, now - 40 * DAY, exe="c.exe", minutes=500)
        self.assertEqual(ex.default_exe(con), "b.exe")
        tid = ex.start(con, "x")
        self.assertEqual(ex.show(con, tid)["exe"], "b.exe")


if __name__ == "__main__":
    unittest.main()
