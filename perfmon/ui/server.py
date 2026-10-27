"""Local HTTP server for the UI: static files + JSON API over analyze.py (read-only DB)."""
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import analyze, config, db, diagreport

STATIC = Path(__file__).parent / "static"


def _f(q, k, default=None):
    v = q.get(k)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except ValueError:
        return analyze.parse_time(v)


def _since(q, default="14d"):
    return analyze.parse_time(q.get("since") or default)


ROUTES = {
    "latest": lambda con, q: analyze.latest(con),
    "series": lambda con, q: analyze.series(con, _f(q, "from"), _f(q, "to", time.time()),
                                            [c for c in q.get("cols", "").split(",") if c],
                                            int(q.get("points", 1500))),
    "frames": lambda con, q: analyze.frame_series(con, _f(q, "from"), _f(q, "to", time.time()),
                                                  int(q["session"]) if q.get("session") else None,
                                                  int(q.get("points", 1500))),
    "sessions": lambda con, q: analyze.sessions(con, int(q.get("limit", 50))),
    "game_proc": lambda con, q: diagreport.game_series(con, int(q["session"]), int(q.get("points", 1500))),
    "games": lambda con, q: analyze.game_list(con),
    "session": lambda con, q: analyze.session_report(con, int(q["id"])),
    "range": lambda con, q: analyze.range_report(con, _f(q, "from"), _f(q, "to", time.time())),
    "moment": lambda con, q: analyze.moment(con, _f(q, "ts"), int(q.get("window", 30))),
    "incidents": lambda con, q: analyze.incidents(con, _since(q), [k for k in q.get("kinds", "").split(",") if k] or None),
    "health": lambda con, q: analyze.health_overview(con, int(q.get("days", 14))),
    "compare": lambda con, q: analyze.compare(con, q.get("exe") or None, q.get("by", "week"), _since(q, "180d"),
                                              q.get("before") or None, q.get("after") or None),
    "markers": lambda con, q: analyze.timeline_markers(con, q.get("exe") or None, _since(q, "180d"),
                                                       diag=q.get("diag") == "1"),
    "points": lambda con, q: analyze.session_points(con, q.get("exe") or None, _since(q, "180d")),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/"):
            name = u.path[5:]
            fn = ROUTES.get(name)
            if not fn:
                return self._send(404, b'{"error":"not found"}', "application/json")
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not config.DB_PATH.exists():
                return self._send(503, b'{"error":"no data yet"}', "application/json")
            con = db.connect(readonly=True)
            try:
                res = fn(con, q)
                body = json.dumps(res, ensure_ascii=False, default=str).encode("utf-8")
                return self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")
            finally:
                con.close()
        rel = u.path.lstrip("/") or "index.html"
        root = STATIC.resolve()
        p = (STATIC / rel).resolve()
        if root not in p.parents or not p.is_file():  # SPA fallback + no path traversal
            p = root / "index.html"
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, p.read_bytes(), ctype)


def serve(port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
