import json
import unittest

from perfmon import regression
from tests.test_bottleneck import new_db

CTX = {"adrenalin": "26.10.1", "game_version": "1.0", "os_build": "26200.1", "bios": "F35", "exe_mtime": 1.0,
       "steam": {"buildid": "100", "last_updated": 5.0}, "gpus": [{"name": "RX", "driver": "32.0.1"}]}


def add(con, sid, fps, low=95.0, spm=5.0, minutes=30, ctx=None, exe="cod.exe", lat=10.0, start=None):
    start = start if start is not None else 1.7e9 + sid * 86400
    gp = {"seconds": minutes * 60, "usable": minutes * 60 >= 120, "fps_avg": fps, "fps_1low": low, "stutters_per_min": spm}
    con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts, end_reason, summary, context) VALUES (?,?,?,?,?,?,?)",
                (sid, exe, start, start + minutes * 60 + 60, "exit",
                 json.dumps({"gameplay": gp, "present": {"click_lat_avg": lat}}), json.dumps(ctx or CTX)))


def baseline(con, n=5):
    for i, fps in enumerate([140, 142, 138, 141, 139, 140, 143, 137][:n], 1):
        add(con, i, fps)


class RegressionTest(unittest.TestCase):
    def test_regression_with_context_changes(self):
        con = new_db()
        baseline(con)
        ctx = dict(CTX, adrenalin="26.10.2", exe_mtime=2.0, steam={"buildid": "101", "last_updated": 9.0},
                   gpus=[{"name": "RX", "driver": "32.0.2"}], game_settings={"preset": "ultra"})
        add(con, 10, 120, low=80, ctx=ctx)
        con.execute("INSERT INTO incidents(ts, kind, severity, title, details) VALUES (?,?,?,?,?)",
                    (1.7e9 + 7 * 86400, "config_change", "info", "Zmiana konfiguracji: bios",
                     json.dumps({"bios": {"before": "F35", "after": "F36"}})))
        a = regression.assess(con, 10)
        self.assertEqual(a["verdict"], "regression")
        v = {m["key"]: m["verdict"] for m in a["metrics"]}
        self.assertEqual((v["fps_avg"], v["fps_1low"], v["stutters_per_min"]), ("regression", "regression", "noise"))
        keys = {c["key"] for c in a["changes"]}
        self.assertEqual(keys, {"adrenalin", "steam.buildid", "gpus[RX].driver", "bios"})   # volatile keys skipped
        self.assertEqual(a["last_good"]["session"], 5)
        self.assertIn("26.10.1 → 26.10.2", a["message"])
        f = regression.finding(a)
        self.assertEqual(f["severity"], "warning")
        self.assertIn("1% low", f["title"])   # the biggest drop leads

    def test_last_good_skips_a_bad_session_in_a_small_baseline(self):
        con = new_db()
        add(con, 1, 140)
        add(con, 2, 141)
        add(con, 3, 110, low=70, ctx=dict(CTX, adrenalin="26.10.2"))   # the driver update already hurt
        add(con, 10, 112, low=71, ctx=dict(CTX, adrenalin="26.10.2"))
        a = regression.assess(con, 10)
        self.assertEqual(a["last_good"]["session"], 2)
        self.assertEqual([c["key"] for c in a["changes"]], ["adrenalin"])

    def test_new_context_keys_diff_generically(self):
        con = new_db()
        for i in range(1, 5):
            add(con, i, 140, ctx=dict(CTX, game_settings={"preset": "high"}))
        add(con, 10, 120, ctx=dict(CTX, game_settings={"preset": "ultra"}))
        self.assertEqual([c["key"] for c in regression.assess(con, 10)["changes"]], ["game_settings.preset"])

    def test_within_noise_and_improvement(self):
        con = new_db()
        baseline(con)
        add(con, 10, 136)
        a = regression.assess(con, 10)
        self.assertEqual(a["verdict"], "noise")
        self.assertIn("granicach", a["message"])
        add(con, 11, 175)
        self.assertEqual(regression.assess(con, 11)["verdict"], "improvement")

    def test_noisy_baseline_widens_the_band(self):
        con = new_db()
        for i, fps in enumerate([100, 140, 120, 160, 110, 150], 1):
            add(con, i, fps)
        add(con, 10, 112)   # -14% vs the median, but sessions swing more than that
        self.assertEqual({m["key"]: m["verdict"] for m in regression.assess(con, 10)["metrics"]}["fps_avg"], "noise")

    def test_insufficient(self):
        con = new_db()
        baseline(con, 2)
        add(con, 10, 100)
        self.assertEqual(regression.assess(con, 10)["verdict"], "insufficient")
        baseline_con = new_db()
        baseline(baseline_con)
        add(baseline_con, 10, 100, minutes=5)   # too little gameplay to judge
        self.assertEqual(regression.assess(baseline_con, 10)["verdict"], "insufficient")

    def test_other_games_and_short_sessions_not_in_baseline(self):
        con = new_db()
        baseline(con)
        add(con, 6, 60, exe="bf6.exe")
        add(con, 7, 50, minutes=5)
        add(con, 8, 141, exe="sp26-cod.exe")   # alias of the same game
        add(con, 10, 140)
        a = regression.assess(con, 10)
        self.assertEqual(a["baseline"]["sessions"], [1, 2, 3, 4, 5, 8])

    def test_attach_replaces_all_clear(self):
        con = new_db()
        baseline(con)
        add(con, 10, 110, low=80)
        row = dict(con.execute("SELECT * FROM sessions WHERE id=10").fetchone())
        rep = {"findings": [{"severity": "info", "title": regression.NO_PROBLEMS, "detail": ""}]}
        regression.attach(con, rep, row)
        self.assertEqual([f["kind"] for f in rep["findings"]], ["perf_regression"])
        self.assertEqual(rep["regression"]["verdict"], "regression")

    def test_overview_newest_first(self):
        con = new_db()
        baseline(con)
        add(con, 10, 110)
        self.assertEqual([a["session"] for a in regression.overview(con)][:2], [10, 5])


if __name__ == "__main__":
    unittest.main()
