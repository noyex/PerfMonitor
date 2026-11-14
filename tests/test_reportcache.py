import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from perfmon import analyze as an
from perfmon import db, reportcache
from tests.test_bottleneck import add_session, new_db


def file_db(path):
    con = sqlite3.connect(path, check_same_thread=False)
    db.migrate(con)
    con.row_factory = sqlite3.Row
    return con


class ReportCacheTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.build = reportcache.cached(lambda con, sid: self.calls.append(sid) or {"sid": sid, "n": len(self.calls)})
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "perf.db")
        self.con = file_db(self.path)
        self.con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts, end_reason, summary) VALUES "
                         "(1, 'game.exe', 1e9, 1e9 + 3600, 'exit', '{}')")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_finished_session_built_once(self):
        a = self.build(self.con, 1)
        b = self.build(self.con, 1)
        self.assertEqual(self.calls, [1])
        self.assertEqual(a, b)
        self.assertEqual(self.build(self.con, 1, cache=False)["n"], 2)

    def test_readonly_connection_writes_through_a_writer(self):
        ro = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            self.build(ro, 1)
            self.build(ro, 1)
        finally:
            ro.close()
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM report_cache").fetchone()[0], 1)

    def test_running_and_fresh_sessions_not_cached(self):
        now = time.time()
        self.con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts) VALUES (2, 'g.exe', ?, NULL)", (now - 60,))
        self.con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts) VALUES (3, 'g.exe', ?, ?)", (now - 900, now - 5))
        for sid in (2, 2, 3, 3):
            self.build(self.con, sid)
        self.assertEqual(self.calls, [2, 2, 3, 3])

    def test_late_data_and_code_changes_invalidate(self):
        self.con.isolation_level = None   # autocommit: the cache writer is a second connection
        self.build(self.con, 1)
        self.con.execute("INSERT INTO incidents(ts, kind, severity, title) VALUES (?, 'app_crash', 'serious', 'x')",
                         (1e9 + 3601,))
        self.build(self.con, 1)
        self.con.execute("INSERT INTO etw_captures(ts, trigger, status) VALUES (?, 'stutter_burst', 'captured')",
                         (1e9 + 100,))
        self.build(self.con, 1)
        self.con.execute("UPDATE etw_captures SET status='analyzed'")
        self.build(self.con, 1)
        self.con.execute("UPDATE sessions SET end_reason='crash' WHERE id=1")
        self.build(self.con, 1)
        self.assertEqual(len(self.calls), 5)
        old = reportcache._code_hash
        try:
            reportcache._code_hash = "other"
            self.build(self.con, 1)
        finally:
            reportcache._code_hash = old
        self.assertEqual(len(self.calls), 6)
        self.build(self.con, 1)   # rebuilt under the other version: rebuilt again under the current one
        self.assertEqual(len(self.calls), 7)

    def test_in_memory_db_uses_the_same_connection(self):
        con = new_db()
        con.execute("INSERT INTO sessions(id, exe, start_ts, end_ts) VALUES (1, 'g.exe', 1e9, 1e9 + 60)")
        self.build(con, 1)
        self.build(con, 1)
        self.assertEqual(self.calls, [1])


class SessionReportCacheTest(unittest.TestCase):
    def test_cached_report_equals_fresh(self):
        con = new_db()
        add_session(con, 1, 1.7e9, minutes=5)
        cached = an.session_report(con, 1)
        again = an.session_report(con, 1)
        fresh = json.loads(json.dumps(an.session_report(con, 1, cache=False), ensure_ascii=False, default=str))
        self.assertEqual(cached, fresh)
        self.assertEqual(again, fresh)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM report_cache").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
