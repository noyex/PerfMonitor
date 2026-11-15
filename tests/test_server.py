import gzip
import http.client
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from perfmon import banners, config, db, uiviews
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

    def test_problems_fold_repeats_and_gzip(self):
        with mock.patch.object(uiviews.time, "time", return_value=1.7e9 + 2000):
            r, body = self.get("/api/problems?days=1", **{"Accept-Encoding": "gzip"})
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")
        d = json.loads(gzip.decompress(body))
        self.assertEqual(len(d["items"]), uiviews.PROBLEM_ROWS)   # 40 crashed programs + the memory episode, capped
        self.assertEqual((d["items"][0]["group"], d["items"][0]["app"]), ("crash", "some-long-application-name-39.exe"))
        self.assertEqual(d["agent"], {"note": None, "test": None})

    def test_banner_is_served_from_the_cache_and_missing_is_404(self):
        jpg = b"\xff\xd8" + b"x" * 5000
        with mock.patch.object(config, "DATA_DIR", Path(self.dir)), \
                mock.patch.object(banners, "_local", return_value=None), \
                mock.patch.object(banners, "_download", side_effect=lambda appid: {"730": jpg, "1": b""}.get(appid)) as dl:
            for _ in range(2):
                r, body = self.get("/api/banner?appid=730")
                self.assertEqual((r.status, r.getheader("Content-Type"), body), (200, "image/jpeg", jpg))
            self.assertEqual(self.get("/api/banner?appid=1")[0].status, 404)
            self.assertEqual(self.get("/api/banner?appid=1")[0].status, 404)
            self.assertEqual(self.get("/api/banner?appid=../x")[0].status, 404)
            for _ in range(2):   # Steam not reachable: asked again next time
                self.assertEqual(self.get("/api/banner?appid=2")[0].status, 404)
        self.assertEqual([c.args[0] for c in dl.call_args_list], ["730", "1", "2", "2"])   # hits and misses are remembered

    def test_banner_is_read_from_the_steam_library_cache(self):
        jpg = b"\xff\xd8" + b"y" * 5000
        steam = Path(self.dir) / "Steam"
        folder = steam / "appcache" / "librarycache" / "440" / "42dd64e5"
        folder.mkdir(parents=True)
        (folder / "library_header.jpg").write_bytes(jpg)
        (folder.parent / "logo.jpg").write_bytes(jpg[:100])
        with mock.patch.object(banners, "_steam_dirs", return_value=[str(steam)]), \
                mock.patch.object(banners, "_download") as dl:
            self.assertEqual(banners._local("440"), jpg)
            self.assertIsNone(banners._local("441"))
        dl.assert_not_called()

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
        self.assertEqual(self.request("OPTIONS", "/api/game_target", shell).status, 403)
        self.assertEqual(self.request("POST", "/api/game_target", shell, b'{}').status, 403)
        self.assertEqual(self.request("POST", "/api/game_target", None, b'{}').status, 400)   # same origin

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

            r = self.request("OPTIONS", "/api/game_target", shell, **{"Access-Control-Request-Method": "POST"})
            self.assertEqual(r.status, 204)
            self.assertEqual(r.getheader("Access-Control-Allow-Origin"), shell)
            self.assertIn(server.TOKEN_HEADER, r.getheader("Access-Control-Allow-Headers"))
            self.assertEqual(self.request("OPTIONS", "/api/game_target", "http://evil.example").status, 403)

            body = b'{}'   # no game named: 400 means the request reached the handler
            self.assertEqual(self.request("POST", "/api/game_target", shell, body).status, 403)
            self.assertEqual(self.request("POST", "/api/game_target", shell, body, **{server.TOKEN_HEADER: "wrong"}).status, 403)
            # non-ASCII token: refused, not a crash in compare_digest
            self.assertEqual(self.request("POST", "/api/game_target", shell, body, **{server.TOKEN_HEADER: "é"}).status, 403)
            r = self.request("POST", "/api/game_target", shell, body, **{server.TOKEN_HEADER: token})
            self.assertEqual(r.status, 400)
            self.assertEqual(r.getheader("Access-Control-Allow-Origin"), shell)
            self.assertEqual(self.request("POST", "/api/game_target", "http://evil.example", body,
                                          **{server.TOKEN_HEADER: token}).status, 403)


if __name__ == "__main__":
    unittest.main()
