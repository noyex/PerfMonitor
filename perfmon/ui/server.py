"""Local HTTP server for the UI: static files + JSON API over analyze.py (read-only DB)."""
import gzip
import hmac
import json
import mimetypes
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import analyze, banners, config, db, notes, ocanalyze, ocassist, uiviews, updater

STATIC = Path(__file__).parent / "static"

# What the window reads. The rest of the data is for the user's AI agent, through the `pm` CLI.
ROUTES = {
    "latest": lambda con, q: analyze.latest(con),
    "overview": lambda con, q: uiviews.home(con),
    "games": lambda con, q: uiviews.games(con),
    "game": lambda con, q: uiviews.game(con, q["exe"]),
    "problems": lambda con, q: uiviews.problems(con, int(q.get("days", 14))),
    "oc": lambda con, q: ocanalyze.overview(con),
    "oc_next": lambda con, q: ocassist.next_step(con),
    "agent_card": lambda con, q: notes.card(con, general=True),   # the note about the whole PC
}


GZIP_MIN = 1024
# The desktop shell (src-tauri) serves the UI from its own origin and starts this server with a per-launch token in
# PERFMON_UI_TOKEN. Only then does that origin get CORS on the API, and its POSTs must carry the token: every Tauri 2
# app on Windows shares the origin http://tauri.localhost (https:// with useHttpsScheme). Never "*".
TAURI_ORIGINS = ("http://tauri.localhost", "https://tauri.localhost")
TOKEN_HEADER = "X-PerfMon-Token"
UI_TOKEN = os.environ.get("PERFMON_UI_TOKEN", "")


class _Pool:
    """Read-only connections reused across requests: opening one costs ~7 ms (schema shim). A connection is dropped
    when the collector migrated the schema, since its shim views were built for the schema at open time."""

    def __init__(self, size=4):
        self.size, self.free, self.lock = size, [], threading.Lock()

    def get(self):
        with self.lock:
            while self.free:
                con, ver = self.free.pop()
                try:
                    if con.execute("PRAGMA main.schema_version").fetchone()[0] == ver:
                        return con, ver
                except sqlite3.Error:
                    pass
                con.close()
        con = db.connect(readonly=True)
        return con, con.execute("PRAGMA main.schema_version").fetchone()[0]

    def put(self, con, ver):
        with self.lock:
            if len(self.free) < self.size:
                self.free.append((con, ver))
                return
        con.close()


POOL = _Pool()
_STATIC_GZ = {}   # (path, mtime_ns) -> gzipped bytes


def _local_host(host):
    return urlparse(f"//{host}").hostname in ("127.0.0.1", "localhost")


POSTS = {"/api/game_target": uiviews.set_target,   # the only write endpoints
         "/api/update": lambda con, body: updater.start_install()}
NO_DB_ROUTES = {
    "update": lambda q: updater.status(q.get("force") == "1"),   # GitHub
    "agent": lambda q: {"guide": str(config.AGENT_GUIDE), "cli": str(config.cli_path())},   # "Zapytaj agenta" prompt
}
NO_DB = {"/api/update"}   # POSTs that never touch the database (work before the first sample, never wait on a lock)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive: a page load fires a dozen API calls

    def log_message(self, *a):
        pass

    def _cors_origin(self):
        """The request's Origin when this server belongs to the desktop shell, the request comes from its origin and the
        path is the API, else None."""
        origin = self.headers.get("Origin")
        return origin if UI_TOKEN and origin in TAURI_ORIGINS and self.path.startswith("/api/") else None

    def _token_ok(self):
        return bool(UI_TOKEN) and hmac.compare_digest(self.headers.get(TOKEN_HEADER, "").encode(), UI_TOKEN.encode())

    def _gzip_ok(self):
        return "gzip" in self.headers.get("Accept-Encoding", "")

    def _send(self, code, body: bytes, ctype, cache="no-store", etag=None, gz=None):
        """gz: pre-compressed body to send instead when the client accepts gzip."""
        if gz is None and len(body) >= GZIP_MIN and self._gzip_ok():
            gz = gzip.compress(body, 5)
        use_gz = gz is not None and self._gzip_ok()
        if use_gz:
            body = gz
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        cors = self._cors_origin()
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
        if etag:
            self.send_header("ETag", etag)
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Vary", "Accept-Encoding, Origin" if cors else "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """CORS preflight: only for the desktop shell's origin; anyone else gets no CORS headers, so the browser blocks."""
        origin = self._cors_origin()
        self.send_response(204 if origin and _local_host(self.headers.get("Host", "")) else 403)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        """The write endpoints (POSTS). JSON-only + same-origin check: a web page in the browser
        cannot post here (non-simple content type needs a CORS preflight); the desktop shell's page may, with its token."""
        u = urlparse(self.path)
        self.close_connection = True   # early refusals leave the body unread: never reuse this connection
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if not _local_host(host):
            return self._send(403, b'{"error":"forbidden"}', "application/json")
        handler = POSTS.get(u.path)
        if not handler:
            return self._send(404, b'{"error":"not found"}', "application/json")
        cross_site = origin and urlparse(origin).netloc != host
        if not self.headers.get("Content-Type", "").startswith("application/json") or (
                cross_site and not (self._cors_origin() and self._token_ok())):
            return self._send(403, b'{"error":"forbidden"}', "application/json")
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("oczekiwano obiektu JSON")
            if u.path in NO_DB:
                res = handler(None, body)
            else:
                con = db.connect()
                try:
                    res = handler(con, body)
                finally:
                    con.close()
            return self._send(200, json.dumps(res).encode(), "application/json")
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        except Exception as e:
            return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/"):
            if not _local_host(self.headers.get("Host", "")):   # DNS rebinding: only 127.0.0.1 / localhost
                return self._send(403, b'{"error":"forbidden"}', "application/json")
            name = u.path[5:]
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if name == "banner":   # a game's Steam banner, see perfmon/banners.py
                img = banners.get(q.get("appid"))
                if img is None:
                    return self._send(404, b'{"error":"not found"}', "application/json")
                return self._send(200, img, "image/jpeg", cache="private, max-age=604800")
            if name in NO_DB_ROUTES:   # not the database: works before the first sample too
                return self._send(200, json.dumps(NO_DB_ROUTES[name](q), ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            fn = ROUTES.get(name)
            if not fn:
                return self._send(404, b'{"error":"not found"}', "application/json")
            if not config.DB_PATH.exists():
                return self._send(503, b'{"error":"no data yet"}', "application/json")
            con, ver = POOL.get()
            try:   # back to the pool before sending: the next keep-alive request may arrive right after the body
                body, err = json.dumps(fn(con, q), ensure_ascii=False, default=str).encode("utf-8"), None
            except Exception as e:
                body, err = None, e
            finally:
                POOL.put(con, ver)
            if err is not None:
                return self._send(500, json.dumps({"error": f"{type(err).__name__}: {err}"}).encode(), "application/json")
            return self._send(200, body, "application/json; charset=utf-8")
        rel = u.path.lstrip("/") or "index.html"
        root = STATIC.resolve()
        p = (STATIC / rel).resolve()
        if root not in p.parents or not p.is_file():  # SPA fallback + no path traversal
            p = root / "index.html"
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        # revalidate every time (files change with an update, names are not versioned), but a match costs a 304
        st = p.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if etag in self.headers.get("If-None-Match", ""):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = p.read_bytes()
        gz = None
        if len(body) >= GZIP_MIN and not ctype.startswith(("image/", "font/")) and self._gzip_ok():
            key = (str(p), st.st_mtime_ns)
            gz = _STATIC_GZ.get(key)
            if gz is None:
                gz = _STATIC_GZ[key] = gzip.compress(body, 9)
        self._send(200, body, ctype, cache="no-cache", etag=etag, gz=gz)


def _server(port: int):
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int):
    srv = _server(port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    """Serve forever (the desktop shell starts its own server this way): python -m perfmon.ui.server [port]"""
    port = int(sys.argv[1]) if len(sys.argv) > 1 else config.load()["ui_port"]
    _server(port).serve_forever()


if __name__ == "__main__":
    main()
