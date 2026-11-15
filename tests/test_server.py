import gzip
import http.client
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from perfmon import config, db
from perfmon.ui import server


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        path = Path(cls.dir) / "perf.db"
        con = sqlite3.connect(path)
        db.migrate(con)
        for i in range(30):
            con.execute("INSERT INTO incidents(ts, end_ts, kind, severity, title, details) VALUES (?,?,?,?,?,?)",
                        (1.7e9 + 10 * i, 1.7e9 + 10 * i + 2, "commit_full", "serious", "commit", json.dumps({"x": "y" * 500})))
        for i in range(40):
            con.execute("INSERT INTO incidents(ts, kind, severity, title) VALUES (?,?,?,?)",
                        (1.7e9 + 1000 + i, "app_crash", "serious", f"Crash aplikacji: some-long-application-name-{i}.exe"))
        con.commit()
        con.close()
        cls.patch = mock.patch.object(config, "DB_PATH", path)
        cls.patch.start()
        server.POOL = server._Pool()
        cls.srv = server.serve(0)
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        for con, _ in server.POOL.free:
            con.close()
        cls.patch.stop()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def get(self, path, conn=None, **headers):
        c = conn or http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", **headers})
        r = c.getresponse()
        return r, r.read()

    def test_incidents_compact_grouped_gzipped(self):
        r, body = self.get("/api/incidents?since=1600000000", **{"Accept-Encoding": "gzip"})
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")
        rows = json.loads(gzip.decompress(body))
        self.assertEqual(len(rows), 41)
        self.assertEqual(rows[0]["count"], 30)
        self.assertNotIn("details", rows[0])
        r, body = self.get(f"/api/incident?id={rows[0]['id']}")
        self.assertIsNone(r.getheader("Content-Encoding"))   # no Accept-Encoding: plain
        self.assertEqual(json.loads(body)["details"], {"x": "y" * 500})

    def test_keep_alive_reuses_pooled_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        self.get("/api/games", conn=c)
        with mock.patch.object(server.db, "connect", wraps=db.connect) as opened:
            for _ in range(3):
                r, body = self.get("/api/games", conn=c)
                self.assertEqual(r.status, 200, body)
        self.assertEqual(opened.call_count, 0)

    def test_agent_paths_without_database(self):
        with mock.patch.object(config, "DB_PATH", Path(self.dir) / "missing.db"):
            r, body = self.get("/api/agent")
        self.assertEqual(r.status, 200, body)
        paths = json.loads(body)
        self.assertEqual(Path(paths["guide"]), config.AGENT_GUIDE)
        self.assertEqual(Path(paths["cli"]).name, "pm.cmd")   # source checkout; pm.exe in the installed build

    def test_static_etag_revalidation(self):
        r, body = self.get("/lib.js", **{"Accept-Encoding": "gzip"})
        self.assertEqual(r.getheader("Cache-Control"), "no-cache")
        self.assertTrue(body)
        r2, body2 = self.get("/lib.js", **{"If-None-Match": r.getheader("ETag")})
        self.assertEqual((r2.status, body2), (304, b""))

    def test_post_refusal_closes_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("POST", "/api/nope", body=b"{}", headers={"Host": f"127.0.0.1:{self.port}",
                                                             "Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        self.assertEqual((r.status, r.getheader("Connection")), (404, "close"))

    def request(self, method, path, origin=None, body=None, **headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {"Host": f"127.0.0.1:{self.port}", **({"Origin": origin} if origin else {}), **headers}
        if body is not None:
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        r.read()
        c.close()
        return r

    def test_no_cors_without_a_shell_token(self):
        """Without PERFMON_UI_TOKEN (browser mode, pywebview) the server behaves like before the desktop shell."""
        shell = "http://tauri.localhost"
        r, _ = self.get("/api/games", Origin=shell)
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(self.request("OPTIONS", "/api/test", shell).status, 403)
        self.assertEqual(self.request("POST", "/api/test", shell, b'{"action": "nope"}').status, 403)
        self.assertEqual(self.request("POST", "/api/test", None, b'{"action": "nope"}').status, 400)   # same origin

    def test_shell_token_gates_cors_and_posts(self):
        shell, token = "http://tauri.localhost", "0123456789abcdef0123456789abcdef"
        with mock.patch.object(server, "UI_TOKEN", token):
            r, _ = self.get("/api/games", Origin=shell)
            self.assertEqual(r.getheader("Access-Control-Allow-Origin"), shell)
            self.assertIn("Origin", r.getheader("Vary"))
            for origin in ("http://evil.example", "http://127.0.0.1:1"):
                r, _ = self.get("/api/games", Origin=origin)
                self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
            r, _ = self.get("/lib.js", Origin=shell)   # static files are not part of the API
            self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

            r = self.request("OPTIONS", "/api/test", shell, **{"Access-Control-Request-Method": "POST"})
            self.assertEqual(r.status, 204)
            self.assertEqual(r.getheader("Access-Control-Allow-Origin"), shell)
            self.assertIn(server.TOKEN_HEADER, r.getheader("Access-Control-Allow-Headers"))
            self.assertEqual(self.request("OPTIONS", "/api/test", "http://evil.example").status, 403)

            body = b'{"action": "nope"}'   # unknown action: 400 means the request reached the handler
            self.assertEqual(self.request("POST", "/api/test", shell, body).status, 403)
            self.assertEqual(self.request("POST", "/api/test", shell, body, **{server.TOKEN_HEADER: "wrong"}).status, 403)
            # non-ASCII token: refused, not a crash in compare_digest
            self.assertEqual(self.request("POST", "/api/test", shell, body, **{server.TOKEN_HEADER: "é"}).status, 403)
            r = self.request("POST", "/api/test", shell, body, **{server.TOKEN_HEADER: token})
            self.assertEqual(r.status, 400)
            self.assertEqual(r.getheader("Access-Control-Allow-Origin"), shell)
            self.assertEqual(self.request("POST", "/api/test", "http://evil.example", body,
                                          **{server.TOKEN_HEADER: token}).status, 403)


if __name__ == "__main__":
    unittest.main()
