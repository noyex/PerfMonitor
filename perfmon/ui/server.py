"""Local HTTP server for the UI: static files + JSON API over analyze.py (read-only DB)."""
import gzip
import json
import mimetypes
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import advisor, analyze, config, db, diagreport, experiments, ocanalyze, ocassist, regression, updater

STATIC = Path(__file__).parent / "static"


def _f(q, k, default=None):
    v = q.get(k)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except ValueError:
        return analyze.parse_time(v)


def _span(q):
    """(from, to) of a chart route; a missing "from" means the last hour rather than a TypeError."""
    t1 = _f(q, "to", time.time())
    return _f(q, "from", t1 - 3600), t1


def _since(q, default="14d"):
    return analyze.parse_time(q.get("since") or default)


ROUTES = {
    "latest": lambda con, q: analyze.latest(con),
    "overview": lambda con, q: analyze.overview(con),
    "series": lambda con, q: analyze.series(con, *_span(q),
                                            [c for c in q.get("cols", "").split(",") if c],
                                            int(q.get("points", 1500))),
    "net_series": lambda con, q: analyze.net_series(con, *_span(q), int(q.get("points", 1500))),
    "frames": lambda con, q: analyze.frame_series(con, _f(q, "from"), _f(q, "to"),
                                                  int(q["session"]) if q.get("session") else None,
                                                  int(q.get("points", 1500))),
    "sessions": lambda con, q: analyze.sessions(con, int(q.get("limit", 50))),
    "diag_markers": lambda con, q: diagreport.diag_markers(con, *_span(q)),
    "game_proc": lambda con, q: diagreport.game_series(con, int(q["session"]), int(q.get("points", 1500))),
    "games": lambda con, q: analyze.game_list(con),
    "session": lambda con, q: analyze.session_report(con, int(q["id"])),
    "range": lambda con, q: analyze.range_report(con, *_span(q)),
    "moment": lambda con, q: analyze.moment(con, _f(q, "ts", time.time()), int(q.get("window", 30))),
    "incidents": lambda con, q: analyze.incidents(con, _since(q), [k for k in q.get("kinds", "").split(",") if k] or None),
    "incident": lambda con, q: analyze.incident(con, int(q["id"])),
    "advise": lambda con, q: advisor.advise(con),
    "health": lambda con, q: analyze.health_overview(con, int(q.get("days", 14))),
    "compare": lambda con, q: analyze.compare(con, q.get("exe") or None, q.get("by", "week"), _since(q, "180d"),
                                              q.get("before") or None, q.get("after") or None),
    "markers": lambda con, q: analyze.timeline_markers(con, q.get("exe") or None, _since(q, "180d"),
                                                       diag=q.get("diag") == "1"),
    "points": lambda con, q: analyze.session_points(con, q.get("exe") or None, _since(q, "180d")),
    "oc": lambda con, q: ocanalyze.overview(con),
    "oc_compare": lambda con, q: ocanalyze.compare_profiles(con, q.get("exe") or None, q.get("since") or "180d"),
    "oc_vf": lambda con, q: ocanalyze.vf_curve(con, _pid(q), q.get("since") or "30d"),
    "oc_cores": lambda con, q: ocanalyze.per_core(con, _pid(q), q.get("since") or "30d"),
    "tests": lambda con, q: experiments.overview(con),
    "oc_stress": lambda con, q: ocanalyze.stress_runs(con, int(q.get("limit", 30))),
    "oc_next": lambda con, q: ocassist.next_step(con),
    "regressions": lambda con, q: regression.overview(con, q.get("exe") or None),
}


def _brief(rows):
    """Incident lists without `details` (the UI shows time, title, severity; /api/incident?id= has the rest): the
    14-day list was 404 KB, mostly ETW and freeze snapshots."""
    return [{k: v for k, v in r.items() if k != "details"} for r in rows or []]


def _compact(name, res):
    if not isinstance(res, (dict, list)):
        return res
    if name == "incidents":
        return _brief(res)
    if name == "health" and isinstance(res, dict):
        return {**res, "diag_incidents": _brief(res.get("diag_incidents"))}
    if name == "session" and isinstance(res, dict) and "incidents" in res:
        return {**res, "incidents": _brief(res["incidents"])}
    return res


GZIP_MIN = 1024


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


def _pid(q):
    return int(q["profile"]) if q.get("profile") else None


def save_profile(con, body):
    """POST /api/oc_profile: create (no id) or update a profile; "activate": true = active from now."""
    pid = body.get("id")
    args = {k: body.get(k) for k in ("name", "cpu", "gpu", "notes", "status")}
    if pid:
        ocanalyze.update_profile(con, int(pid), **args, replace=True)
    else:
        pid = ocanalyze.create_profile(con, args["name"], args["cpu"], args["gpu"], args["notes"],
                                       args["status"] or "testing")
    if body.get("activate"):
        ocanalyze.activate(con, int(pid))
    return {"id": pid}


def test_action(con, body):
    """POST /api/test: {"action": "start", "name", "key"?, "exe"?} | {"action": "stop"} | {"action": "cancel"}."""
    act = body.get("action")
    if act == "start":
        return {"id": experiments.start(con, body.get("name"), body.get("exe") or None, body.get("key") or None)}
    if act == "stop":
        return experiments.stop(con)
    if act == "cancel":
        return {"cancelled": experiments.cancel(con)}
    raise ValueError("nieznana akcja")


POSTS = {"/api/oc_profile": save_profile, "/api/test": test_action,   # the only write endpoints
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
        if etag:
            self.send_header("ETag", etag)
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """The write endpoints (POSTS: UV/OC profiles, tests). JSON-only + same-origin check: a web page in the browser
        cannot post here (non-simple content type needs a CORS preflight that this server never allows)."""
        u = urlparse(self.path)
        self.close_connection = True   # early refusals leave the body unread: never reuse this connection
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if not _local_host(host):
            return self._send(403, b'{"error":"forbidden"}', "application/json")
        handler = POSTS.get(u.path)
        if not handler:
            return self._send(404, b'{"error":"not found"}', "application/json")
        if not self.headers.get("Content-Type", "").startswith("application/json") or (
                origin and urlparse(origin).netloc != host):
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
                body, err = json.dumps(_compact(name, fn(con, q)), ensure_ascii=False, default=str).encode("utf-8"), None
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


def serve(port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
