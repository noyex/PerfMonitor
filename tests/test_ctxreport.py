import json
import sqlite3
import unittest

from perfmon import ctxreport, db


def ctx(hash_, upscaler, snap_ts, driver="26.10.1", **kw):
    return json.dumps({"adrenalin": driver, "system_snapshot_ts": snap_ts,
                       "game_settings": {"hash": hash_, "normalized": {"upscaler": upscaler, "fps_cap": "off"}, **kw}})


class Report(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        db.migrate(self.con)
        info = lambda plan: json.dumps({"power": {"name": plan}, "hags": 2})
        self.con.execute("INSERT INTO system_info VALUES (50, ?)", (info("Zrównoważony"),))
        self.con.execute("INSERT INTO system_info VALUES (150, ?)", (info("Wysoka wydajność"),))
        self.con.execute("INSERT INTO sessions(id, exe, start_ts, context) VALUES (1, 'cod.exe', 100, ?)", (ctx("a", "FSR 3", 50),))
        self.con.execute("INSERT INTO sessions(id, exe, start_ts, context) VALUES (2, 'cod.exe', 200, ?)",
                         (ctx("b", "FSR 4", 150, driver="26.10.2", prev_hash="a", changed={"AMDFidelityFX": ["FSR 3", "FSR 4"]}),))
        self.con.execute("INSERT INTO sessions(id, exe, start_ts, context, kind) VALUES (3, 'cod.exe', 150, '{}', 'stress')")

    def test_changes_vs_previous_session_of_the_game(self):
        row = self.con.execute("SELECT * FROM sessions WHERE id=2").fetchone()
        rep = {"summary": {"settings_changes": [{"ts": 210, "changes": ["limit FPS: off → 237"]}]}}
        cfg = ctxreport.extend(self.con, rep, row)
        self.assertEqual(cfg["previous_session"], 1)   # the stress session in between is skipped
        self.assertEqual(cfg["changes"], {"game": ["upscaler: FSR 3 → FSR 4"], "game_raw_changed": 1,
                                          "system": ["plan zasilania: Zrównoważony → Wysoka wydajność"],
                                          "context": ["Adrenalin: 26.10.1 → 26.10.2"]})
        self.assertEqual([f["title"] for f in rep["findings"]],
                         ["Zmiany od poprzedniej sesji #1", "Zmiana ustawień gry w trakcie sesji"])

    def test_old_session_without_snapshot_reference(self):
        self.con.execute("UPDATE sessions SET context='{}' WHERE id=1")
        row = self.con.execute("SELECT * FROM sessions WHERE id=1").fetchone()
        self.assertEqual(ctxreport.snapshot_at(self.con, {}, row["start_ts"])["power"]["name"], "Zrównoważony")

    def test_annotate_compare(self):
        rows = [{"exe": "cod.exe", "group": "przed", "session_ids": [1]}, {"exe": "cod.exe", "group": "po", "session_ids": [2]}]
        by_id = {r["id"]: {**json.loads(r["context"]), "_start": r["start_ts"]}
                 for r in self.con.execute("SELECT * FROM sessions WHERE id IN (1, 2)")}
        out = ctxreport.annotate_compare(self.con, rows, by_id)
        self.assertIsNone(out[0]["changes"])
        self.assertEqual(out[1]["settings"]["upscaler"], "FSR 4")
        self.assertEqual(out[1]["changes"], ["Adrenalin: 26.10.1 → 26.10.2", "upscaler: FSR 3 → FSR 4",
                                             "plan zasilania: Zrównoważony → Wysoka wydajność"])


if __name__ == "__main__":
    unittest.main()
