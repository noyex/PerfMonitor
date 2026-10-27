import json
import sqlite3
import unittest

from perfmon import db, diagreport


def session(con, sid, exe, start, driver, build):
    con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts, end_reason, context) VALUES (?,?,?,?,?,?)",
                (sid, exe, start, start + 600, "exit", json.dumps({"adrenalin": driver, "steam": {"buildid": build}})))


class ShaderAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        db.migrate(self.con)
        self.con.row_factory = sqlite3.Row
        session(self.con, 1, "game.exe", 1000, "26.9.1", "100")
        session(self.con, 2, "game.exe", 5000, "26.10.1", "100")
        session(self.con, 3, "game.exe", 9000, "26.10.1", "101")
        session(self.con, 4, "game.exe", 13000, "26.10.1", "101")
        mb = 2**20
        # session 4: cache 100 MB, +1 MB between 13050 and 13055
        for ts, size in ((13040, 100 * mb), (13045, 100 * mb), (13050, 100 * mb), (13055, 101 * mb), (13060, 101 * mb)):
            self.con.execute("INSERT INTO game_proc(ts, session, pid, shader_bytes) VALUES (?,?,?,?)", (ts, 4, 1, size))

    def row(self, sid):
        return dict(self.con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())

    def test_first_session_after_change(self):
        f = lambda sid: diagreport.first_session_after_change(self.con, self.row(sid))
        self.assertIsNone(f(1))
        self.assertEqual(f(2), "driver")
        self.assertEqual(f(3), "game_update")
        self.assertEqual(f(4), "none")

    def test_shader_compile_cause(self):
        ext = diagreport.StutterExt(self.con, self.row(4), 13000, 13600)
        self.assertTrue(ext.shader_compile(13052))      # 100 MB before, 101 MB after
        self.assertFalse(ext.shader_compile(13058))     # no growth after 13055
        self.assertFalse(ext.shader_compile(13200))     # no samples around
        causes = ext.refine(13052, [], [])
        self.assertIn("shader_compile", causes)

    def test_shader_summary_and_finding(self):
        sh = diagreport.shader_summary(self.con, self.row(4))
        self.assertEqual(sh, {"first_session_after_change": "none", "cache_mb": 101.0, "cache_growth_mb": 1.0})
        rep = {"stutters": {"cause_counts": {"shader_compile": 4}}, "shader": sh}
        titles = [f["title"] for f in diagreport.findings(rep, self.row(4))]
        self.assertTrue(any("kompilacji shaderów" in t for t in titles), titles)
        rep["shader"] = dict(sh, first_session_after_change="driver")
        titles = [f["title"] for f in diagreport.findings(rep, self.row(4))]
        self.assertIn("Pierwsza sesja po zmianie sterownika GPU", titles)


class CollectorShaderHookTest(unittest.TestCase):
    def test_sample_gpu_fills_shader_bytes_for_watched_game(self):
        from perfmon.collector import Collector

        class Watch:
            latest = staticmethod(lambda: (1.0, 12345))

        class Sampler:
            def sample(self, pid, sid, gpu, now):
                return {"ts": now, "session": sid, "pid": pid}

        class Gpu:
            read = staticmethod(lambda: {})

        c = object.__new__(Collector)
        c.games = {10: {"session": 1}, 20: {"session": 2}}
        c.shader, c.shader_pid, c.game_sampler, c.gpu_procs = Watch(), 20, Sampler(), Gpu()
        rows = []
        c.sample_gpu(5.0, {}, rows)
        by_pid = {r["pid"]: r for r in rows}
        self.assertNotIn("shader_bytes", by_pid[10])
        self.assertEqual(by_pid[20]["shader_bytes"], 12345)


if __name__ == "__main__":
    unittest.main()
