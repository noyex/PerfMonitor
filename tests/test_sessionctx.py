import json
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from perfmon import db, sessionctx

GPU = "AMD Radeon RX 9070 XT"


class FakeCollector:
    def __init__(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        db.migrate(self.con)
        self.lock = threading.RLock()
        self.cfg = {"game_settings_poll_s": 30, "game_modules_settle_s": 120, "game_modules_poll_s": 600}
        self.hw = SimpleNamespace(gpu=None)
        self.games = {}
        self.incidents = []

    def exec(self, sql, args=()):
        with self.lock:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return cur

    def incident(self, kind, severity, title, details=None, ts=None, end_ts=None):
        self.incidents.append((kind, title, details))

    def _merge(self, col, sid, extra):
        r = self.con.execute(f"SELECT {col} FROM sessions WHERE id=?", (sid,)).fetchone()
        d = json.loads(r[0]) if r and r[0] else {}
        d.update(extra)
        self.exec(f"UPDATE sessions SET {col}=? WHERE id=?", (json.dumps(d), sid))

    def merge_summary(self, sid, extra):
        self._merge("summary", sid, extra)

    def merge_context(self, sid, extra):
        self._merge("context", sid, extra)


def info(driver="32.0.1", **kw):
    return {"gpus": [{"name": GPU, "driver": driver}], "power": {"name": "Zrównoważony", "cpu_min_pct": 0}, **kw}


class SystemSnapshot(unittest.TestCase):
    def setUp(self):
        self.col = FakeCollector()
        self.sc = sessionctx.SessionContext(self.col)
        self.col.exec("INSERT INTO sessions(id, exe, path, start_ts, context) VALUES (1, 'cod.exe', 'D:\\cod.exe', 1, '{}')")

    def rows(self):
        return self.col.exec("SELECT COUNT(*) FROM system_info").fetchone()[0]

    def test_quick_snapshot_without_changes_reuses_the_stored_one(self):
        with mock.patch.object(sessionctx.sysinfo, "collect", return_value=info(disk_health=[{"device": "0"}])):
            self.sc.sysinfo(quick=False)
        ts = self.col.exec("SELECT ts FROM system_info").fetchone()[0]
        with mock.patch.object(sessionctx.sysinfo, "collect", return_value=info()) as c:
            self.sc.sysinfo(quick=True, session=1, extra_paths=["D:\\new.exe"])
        self.assertIn("D:\\new.exe", c.call_args.kwargs["game_paths"])
        self.assertEqual(self.rows(), 1)
        self.assertEqual(json.loads(self.col.exec("SELECT context FROM sessions").fetchone()[0])["system_snapshot_ts"], ts)
        self.assertEqual(self.col.incidents, [])

    def test_change_is_stored_with_a_readable_incident(self):
        with mock.patch.object(sessionctx.sysinfo, "collect", return_value=info()):
            self.sc.sysinfo(quick=False)
        with mock.patch.object(sessionctx.sysinfo, "collect", return_value=info(driver="32.0.2")):
            new = self.sc.sysinfo(quick=True, session=1)
        self.assertEqual(self.rows(), 2)
        (kind, title, det), = self.col.incidents
        self.assertEqual(kind, "config_change")
        self.assertEqual(title, f"Zmiana konfiguracji: sterownik GPU {GPU}: 32.0.1 → 32.0.2")
        self.assertEqual(det[f"gpus[{GPU}].driver"]["after"], "32.0.2")
        self.assertIn("power", new)   # quick snapshot carries keys over from the stored one


class DuringGame(unittest.TestCase):
    def setUp(self):
        self.col = FakeCollector()
        self.sc = sessionctx.SessionContext(self.col)
        self.col.exec("INSERT INTO sessions(id, exe, path, start_ts) VALUES (7, 'cod.exe', 'D:\\cod.exe', 1000)")
        self.g = {"session": 7, "exe": "cod.exe", "path": "D:\\cod.exe", "pid": 10, "start": 1000.0, "children": {},
                  "stress": None}
        self.col.games[10] = self.g
        ctx = {"game_settings": {"hash": "a", "normalized": {"upscaler": "FSR 4", "fps_cap": "off"},
                                 "files": [{"path": "C:\\s.txt", "mtime": 1.0}]}}
        with mock.patch.object(sessionctx.threading, "Thread"):
            self.sc.started(self.g, ctx)

    def test_settings_change_mid_session(self):
        snap = {"hash": "b", "normalized": {"upscaler": "FSR 3", "fps_cap": "off"}, "raw": {"AMDFidelityFX": "FSR 3"},
                "files": [{"path": "C:\\s.txt", "mtime": 2.0}]}
        with mock.patch.object(sessionctx.gamesettings, "file_stamp", side_effect=[(("C:\\s.txt", 2.0),)] * 2), \
                mock.patch.object(sessionctx.gamesettings, "snapshot", return_value=snap):
            self.sc.tick(1000.0)   # not due yet
            self.assertEqual(self.col.incidents, [])
            self.sc.tick(1031.0)
        (kind, title, det), = self.col.incidents
        self.assertEqual(kind, "game_settings_change")
        self.assertEqual(title, "Zmiana ustawień gry cod.exe: upscaler: FSR 4 → FSR 3")
        summ = json.loads(self.col.exec("SELECT summary FROM sessions WHERE id=7").fetchone()[0])
        self.assertEqual(summ["settings_changes"][0]["changes"], ["upscaler: FSR 4 → FSR 3"])

    def test_modules_after_settle_and_on_change(self):
        scans = [{"status": "ok", "count": 300, "flagged": [{"name": "Discord"}], "categories": {"overlay": 1}},
                 {"status": "ok", "count": 301, "flagged": [{"name": "Discord"}, {"name": "Steam overlay"}],
                  "categories": {"overlay": 2}}]
        same = {"hash": "a", "normalized": {}, "raw": {}, "files": []}
        with mock.patch.object(sessionctx.gamemods, "scan", side_effect=scans), \
                mock.patch.object(sessionctx.gamesettings, "snapshot", return_value=same):
            self.sc.tick(1060.0)   # still settling
            summ = self.col.exec("SELECT summary FROM sessions WHERE id=7").fetchone()[0]
            self.assertIsNone(summ)
            self.sc.tick(1121.0)
            self.sc.tick(1800.0)
        summ = json.loads(self.col.exec("SELECT summary FROM sessions WHERE id=7").fetchone()[0])
        self.assertEqual(summ["modules"]["procs"]["cod.exe"]["count"], 301)
        self.assertEqual(summ["modules_changes"][0]["changes"], ["cod.exe: załadowano Steam overlay"])


if __name__ == "__main__":
    unittest.main()
