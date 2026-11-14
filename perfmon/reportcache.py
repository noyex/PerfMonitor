"""Persistent cache of session_report for finished sessions (table report_cache in perf.db, zlib JSON).

A finished session's report is a pure function of what is stored about it, so it is built once instead of on every
open (0.3-0.5 s on this PC, 1.3 s under load; the advisor builds six). The key covers everything that can still
change it, so a stale report is never served:
- the code: REPORT_VERSION plus a hash of perfmon/*.py and config.json, so any analysis change invalidates the cache
  even when nobody bumps the constant;
- the session row (end_ts, end_reason, summary and context, rewritten by late crash checks and analysis upgrades);
- data that arrives after the game: incidents and events in the session window, ETW captures analyzed afterwards;
- whether retention has already pruned the session's samples.
Running sessions and sessions that ended less than MIN_AGE_S ago (late WER events, ETW analysis) are never cached.
The UI reads through a read-only connection, so writes go through a separate short-lived writer; a failed write
only costs the next open a rebuild."""
import functools
import hashlib
import json
import sqlite3
import threading
import time
import zlib
from pathlib import Path

from . import config

REPORT_VERSION = "1"   # bump for changes the source hash cannot see (e.g. a new external input)
MIN_AGE_S = 600
_lock = threading.Lock()


def _source_hash():
    h = hashlib.sha1(REPORT_VERSION.encode())
    for p in sorted(Path(__file__).parent.glob("*.py")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


_code_hash = _source_hash()   # at import, so files updated on disk later never pass as the running code


def code_version():
    """REPORT_VERSION + hash of the analysis sources (as loaded: hashed at import) and the user config."""
    try:
        cfg = config.CONFIG_PATH.read_bytes()
    except OSError:
        cfg = b""
    return f"{_code_hash}-{hashlib.sha1(cfg).hexdigest()[:8]}"


def _key(con, row):
    sid, start, end = row["id"], row["start_ts"], row["end_ts"]
    s = hashlib.sha1(json.dumps([row[k] for k in row.keys()], default=str).encode())
    # late data in the session window (all on ts indexes): count and max rowid catch inserts, deletes and updates of
    # a capture's status; events also by max record_id
    for q, a in (("SELECT COUNT(*), MAX(id), MAX(COALESCE(end_ts, 0)), TOTAL(length(details)) FROM incidents "
                  "WHERE ts BETWEEN ? AND ?", (start, end + 5)),
                 ("SELECT COUNT(*), MAX(rowid) FROM events WHERE ts BETWEEN ? AND ?", (start - 60, end + 60)),
                 ("SELECT COUNT(*), MAX(id), GROUP_CONCAT(status) FROM etw_captures WHERE ts BETWEEN ? AND ?",
                  (start - 120, end + 120))):
        try:
            s.update(repr(tuple(con.execute(q, a).fetchone())).encode())
        except sqlite3.Error:
            s.update(b"-")
    # the regression verdict depends on earlier sessions (summary/context rewritten by upgrades) and config changes
    for q, a in (("SELECT COUNT(*), MAX(id), TOTAL(length(summary)), TOTAL(length(context)), TOTAL(end_ts) FROM sessions "
                  "WHERE start_ts < ?", (start,)),
                 ("SELECT COUNT(*), MAX(id) FROM incidents WHERE kind IN ('config_change', 'gpu_settings_change') "
                  "AND ts < ?", (start,))):
        s.update(repr(tuple(con.execute(q, a).fetchone())).encode())
    first = con.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
    s.update(b"pruned" if first is not None and first > start else b"full")
    return f"{code_version()}:{s.hexdigest()}"


def cacheable(row, now=None):
    return bool(row and row["end_ts"] is not None and row["end_ts"] > (row["start_ts"] or 0)
                and (now or time.time()) - row["end_ts"] >= MIN_AGE_S)


def get(con, sid, key):
    try:
        r = con.execute("SELECT key, report FROM main.report_cache WHERE session=?", (sid,)).fetchone()
    except sqlite3.Error:   # table not created yet (the collector migrates on its next start)
        return None
    if not r or r[0] != key:
        return None
    try:
        return json.loads(zlib.decompress(r[1]))
    except (zlib.error, ValueError):
        return None


def _db_file(con):
    for r in con.execute("PRAGMA database_list"):
        if r[1] == "main":
            return r[2] or None
    return None


def put(con, sid, end_ts, key, rep):
    blob = zlib.compress(json.dumps(rep, ensure_ascii=False, default=str).encode("utf-8"), 6)
    path = _db_file(con)
    try:
        with _lock:
            w = sqlite3.connect(path, timeout=0.5) if path else con   # in-memory DB (tests): the same connection
            try:
                w.execute("CREATE TABLE IF NOT EXISTS report_cache (session INTEGER PRIMARY KEY, ts REAL, key TEXT, "
                          "report BLOB)")
                w.execute("INSERT OR REPLACE INTO report_cache VALUES (?,?,?,?)", (sid, end_ts, key, blob))
                w.commit()
            finally:
                if w is not con:
                    w.close()
    except sqlite3.Error:
        pass   # locked or read-only medium: the next open rebuilds it


def cached(build):
    """Decorator for analyze.session_report(con, session_id): serve finished sessions from the cache."""
    @functools.wraps(build)
    def wrapper(con, session_id, cache=True):
        if not cache:
            return build(con, session_id)
        row = con.execute("SELECT * FROM sessions WHERE id=?", (int(session_id),)).fetchone()
        if not isinstance(row, sqlite3.Row) or not cacheable(row):
            return build(con, session_id)
        key = _key(con, row)
        rep = get(con, row["id"], key)
        if rep is None:
            rep = build(con, session_id)
            if "error" not in rep:
                put(con, row["id"], row["end_ts"], key, rep)
                rep = json.loads(json.dumps(rep, ensure_ascii=False, default=str))   # same shape as a cache hit
        return rep
    return wrapper
