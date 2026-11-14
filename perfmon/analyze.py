"""Analysis helpers over the perf.db SQLite database.

Pure functions taking a (preferably read-only) connection and returning
JSON-serializable dicts/lists. Used by the CLI (perfmon.cli) and the web UI.
Human-readable finding strings are in Polish; code and comments are English.
"""
import bisect
import json
import math
import re
import socket
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import bottleneck, crashinfo, diagreport, framestats, gameplay, marks, stuttercause

# --------------------------------------------------------------------------
# Time parsing / generic helpers
# --------------------------------------------------------------------------

_REL = re.compile(r"^-?\s*(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|d|w)$", re.I)
_UNITS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_time(s) -> float:
    """Unix ts, 'YYYY-MM-DD HH:MM[:SS]', 'HH:MM[:SS]' (today, local), '-2h' / '30m' / '7d' (ago), 'now'."""
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip()
    if not t or t.lower() == "now":
        return time.time()
    m = _REL.match(t)
    if m:
        return time.time() - float(m.group(1)) * _UNITS[m.group(2).lower()]
    if re.fullmatch(r"\d{9,11}(\.\d+)?", t):
        return float(t)
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", t):
        parts = [int(x) for x in t.split(":")]
        now = datetime.now()
        dt = now.replace(hour=parts[0], minute=parts[1], second=parts[2] if len(parts) > 2 else 0, microsecond=0)
        return dt.timestamp()
    t2 = t.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(t2, fmt).timestamp()
        except ValueError:
            pass
    raise ValueError(f"Nie rozumiem czasu: {s!r}")


def fmt_ts(ts) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _one(con, sql, args=()):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def _json(s):
    if s is None:
        return None
    try:
        return json.loads(s)
    except Exception:
        return s


def _r(v, n=1):
    if v is None:
        return None
    try:
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return round(v, n)
    except Exception:
        return v


def _pct(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _table_cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


TEXT_SAMPLE_COLS = {"fg_exe", "disk_lat_disk", "extra"}
MAX_AGG_SAMPLE = {"disk_lat_ms", "ui_ms", "dwm_ms", "disk_queue", "hard_faults_s"}
FRAME_AGG = {  # name -> SQL aggregate expression
    "fps": "AVG(fps)", "fps_min": "MIN(fps)", "ft_avg": "AVG(ft_avg)", "ft_p99": "MAX(ft_p99)",
    "ft_max": "MAX(ft_max)", "cpu_busy": "AVG(cpu_busy)", "gpu_busy": "AVG(gpu_busy)",
    "display_lat": "AVG(display_lat)", "frames": "AVG(frames)",
}
KEY_COLS = ["cpu_total", "cpu_core_max", "cpu_clock_mhz", "cpu_temp", "cpu_power_w", "dpc_pct", "interrupt_pct",
            "ram_pct", "ram_used_gb", "commit_gb", "hard_faults_s", "gpu_load", "gpu_vram_mb", "gpu_clock_mhz",
            "gpu_temp", "gpu_hotspot", "gpu_mem_temp", "gpu_power_w", "gpu_fan_rpm", "disk_lat_ms",
            "disk_busy_pct", "ui_ms", "dwm_ms"]

NOT_BACKGROUND = {"system idle process", "system", "idle", "presentmon.exe", "memory compression", "registry"}


# --------------------------------------------------------------------------
# latest / status
# --------------------------------------------------------------------------

def latest(con):
    s = _one(con, "SELECT * FROM samples ORDER BY ts DESC LIMIT 1")
    now = time.time()
    meta = {r["k"]: r["v"] for r in con.execute("SELECT k, v FROM meta")}
    sess = _one(con, "SELECT * FROM sessions WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1")
    if sess:
        sess["summary"] = _json(sess.get("summary"))
        sess["running_s"] = round(now - sess["start_ts"])
    if s:
        s["extra"] = _json(s.get("extra"))
    net = None
    if _has_net(con):
        net = _one(con, "SELECT ts, gw_ms, inet_ms, inet_loss, game_ms FROM net_samples WHERE ts>=? ORDER BY ts DESC LIMIT 1",
                   (now - 10,))
    cnt = _one(con, "SELECT (SELECT COUNT(*) FROM incidents) AS incidents, (SELECT COUNT(*) FROM sessions) AS sessions,"
                    " (SELECT MIN(ts) FROM samples) AS first_sample")
    return {
        "now": now,
        "sample": s,
        "sample_age_s": _r(now - s["ts"]) if s else None,
        "collector_alive": bool(s and now - s["ts"] < 5),
        "admin": meta.get("admin") == "1",
        "host": socket.gethostname(),
        "collector_started": float(meta["collector_started"]) if meta.get("collector_started") else None,
        "collector_stopped": float(meta["collector_stopped"]) if meta.get("collector_stopped") else None,
        "hw": _json(meta.get("hw")),
        "open_session": sess,
        "net": net,
        "counts": cnt,
    }


# share of session time behind each bottleneck kind (metrics key from bottleneck.compute)
_BOTTLENECK_SHARE = {"gpu": "gpu_bound_pct", "gpu_power": "gpu_bound_pct", "cpu": "cpu_bound_pct",
                     "cpu_thread": "cpu_bound_pct", "limit": "limit_pct", "vram": "vram_shared_over_pct"}


_OVERVIEW_LIVE_S = 130          # stutters sent for a running session (chart shows the last 2 minutes)
_BOTTLENECK_TTL_S = 30          # live bottleneck refresh; finished sessions are cached for good
_bottleneck_cache = {}          # session id -> (computed at, result)


def _overview_bottleneck(con, cur, now):
    sid = cur["id"]
    if not cur["running"]:
        b = (_json(_one(con, "SELECT summary FROM sessions WHERE id=?", (sid,))["summary"]) or {}).get("bottleneck")
        if b:
            return b
    hit = _bottleneck_cache.get(sid)
    if hit and (not cur["running"] or now - hit[0] < _BOTTLENECK_TTL_S):
        return hit[1]
    b = bottleneck.compute(con, sid, cur["start_ts"], cur["end_ts"] or now)
    _bottleneck_cache[sid] = (now, b)
    return b


def overview(con, recent=5):
    """Overview screen: the running game session, else the last one with frames, plus the latest sessions.

    A running session counts only while the collector is alive and the game renders (a frame in the last 30 s, or
    started under a minute ago), so a stale row after a crash or a launcher without frames doesn't take the card.
    `current` adds stutter timestamps, the last stutter and the bottleneck ({kind, share, advice})."""
    now = time.time()
    s = _one(con, "SELECT ts FROM samples ORDER BY ts DESC LIMIT 1")
    alive = bool(s and now - s["ts"] < 5)
    rows = sessions(con, recent + 10)
    live = next((r for r in rows if r["running"]), None)
    if live and not (alive and (now - live["start_ts"] < 60 or _one(
            con, "SELECT 1 AS x FROM frames WHERE session=? AND ts>?", (live["id"], now - 30)))):
        live = None
    cur = live or next((r for r in rows if not r["running"] and r.get("frames")), None)
    if cur:
        sid = cur["id"]
        if cur["running"]:
            st = [r["ts"] for r in con.execute("SELECT ts FROM stutters WHERE session=? AND ts>? ORDER BY ts",
                                               (sid, now - _OVERVIEW_LIVE_S))]
            last = _one(con, "SELECT MAX(ts) AS ts FROM stutters WHERE session=?", (sid,))["ts"]
        else:
            st = [r["ts"] for r in con.execute("SELECT ts FROM stutters WHERE session=? ORDER BY ts", (sid,))]
            last = st[-1] if st else None
        cur["stutter_ts"], cur["last_stutter_ts"] = st, last
        b = _overview_bottleneck(con, cur, now)
        cur["bottleneck"] = {"kind": b["kind"], "share": (b.get("metrics") or {}).get(_BOTTLENECK_SHARE.get(b["kind"])),
                             "advice": b.get("advice")} if b else None
    return {
        "now": now,
        "current": cur,
        # sessions without frames that ended normally are launchers / short starts, not games played
        "recent": [r for r in rows if not r["running"] and r is not cur
                   and (r.get("frames") or r["end_reason"] != "exit")][:recent],
    }


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def _ctx(r):
    """sessions.context JSON as dict (column may be missing or null in older DBs)."""
    c = _json(r.get("context")) if hasattr(r, "get") else None
    return c if isinstance(c, dict) else {}


def _driver(ctx):
    return ctx.get("adrenalin") or ctx.get("gpu_driver")


def _eff_ends(con, rows):
    """{session id: effective end ts}. None = still running (end_ts NULL). Sessions with end_ts <= start_ts
    (collector crash / hard reset) get the last frames/samples ts of the session, else start_ts."""
    ends, broken = {}, []
    for r in rows:
        e = r["end_ts"]
        if e is None:
            ends[r["id"]] = None
        elif r["start_ts"] is not None and e <= r["start_ts"]:
            broken.append(r)
        else:
            ends[r["id"]] = e
    for r in broken:  # few; frames PK (session, ts) and samples PK (ts) keep these cheap
        sid, st = r["id"], r["start_ts"]
        nxt = con.execute("SELECT MIN(start_ts) FROM sessions WHERE start_ts>?", (st,)).fetchone()[0] or time.time()
        f = con.execute("SELECT MAX(ts) FROM frames WHERE session=?", (sid,)).fetchone()[0]
        s = con.execute("SELECT MAX(ts) FROM samples WHERE game_session=? AND ts>=? AND ts<=?", (sid, st, nxt)).fetchone()[0]
        ends[sid] = max(st, f or st, s or st)
    return ends


def _flatten_session(r, stutters=None, ends=None):
    summ = _json(r.get("summary")) or {}
    running = r["end_ts"] is None
    end = ends.get(r["id"]) if ends else r["end_ts"]
    estimated = bool(end and r["end_ts"] is not None and end != r["end_ts"])
    d = {
        "id": r["id"], "exe": r["exe"], "start_ts": r["start_ts"], "end_ts": end if r["end_ts"] is not None else None,
        "end_reason": r["end_reason"], "end_estimated": estimated,
        "duration_s": round((end or time.time()) - r["start_ts"]) if r["start_ts"] else None,
        "running": running, "kind": r.get("kind") or "game", "profile_id": r.get("profile_id"),
    }
    for k in ("frames", "fps_avg", "fps_1low", "fps_01low", "ft_median", "ft_p99", "ft_max", "error",
              "app_fps_avg", "app_fps_1low", "app_fps_01low", "frame_gen_pct"):   # app_*: frame generation left out
        d[k] = summ.get(k)
    ctx = _ctx(r)
    d["context"] = ctx or None
    d["gpu_driver"] = _driver(ctx)
    d["game_version"] = ctx.get("game_version")
    if stutters is not None:
        d["stutters"] = stutters
    return d


GAMES_ONLY = "COALESCE(kind, 'game')='game'"   # sessions that are not stress tests / idle soaks


def sessions(con, limit=20):
    rows = _rows(con, f"SELECT * FROM sessions WHERE {GAMES_ONLY} ORDER BY id DESC LIMIT ?", (int(limit),))
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    q = ",".join("?" * len(ids))
    st = {r["session"]: r["n"] for r in con.execute(
        f"SELECT session, COUNT(*) AS n FROM stutters WHERE session IN ({q}) GROUP BY session", ids)}
    ends = _eff_ends(con, rows)
    ext = diagreport.session_extras(con, rows)
    return [{**_flatten_session(r, st.get(r["id"], 0), ends), **ext.get(r["id"], {})} for r in rows]


def game_list(con):
    """Games with at least one session, most played first (cheap list for pickers)."""
    return _rows(con, f"SELECT exe, COUNT(*) AS sessions, MAX(start_ts) AS last_ts FROM sessions WHERE {GAMES_ONLY} "
                      "GROUP BY exe ORDER BY sessions DESC, last_ts DESC")


def _causes_for_stutter(st, samples_ts, samples, procs_ts, procs, game_exe, ext=None):
    ts = st["ts"]
    causes = []
    lo, hi = bisect.bisect_left(samples_ts, ts - 2), bisect.bisect_right(samples_ts, ts + 2)
    win = samples[lo:hi]

    def mx(col):
        v = [s[col] for s in win if s[col] is not None]
        return max(v) if v else None

    if win:
        if (mx("disk_lat_ms") or 0) > 30:   # hard faults alone are normal streaming (45% of seconds in a CoD session)
            causes.append("disk")
        vr = [s["gpu_vram_mb"] / s["gpu_vram_total_mb"] for s in win if s["gpu_vram_mb"] and s["gpu_vram_total_mb"]]
        if vr and max(vr) > 0.95:
            causes.append("vram")
        cm = [s["commit_gb"] / s["commit_limit_gb"] for s in win if s["commit_limit_gb"]]
        if (mx("ram_pct") or 0) > 90 or (cm and max(cm) > 0.92):
            causes.append("ram")
        if any((s["cpu_core_max"] or 0) > 95 and (s["gpu_load"] if s["gpu_load"] is not None else 0) < 85 for s in win):
            causes.append("cpu_thread")
    plo, phi = bisect.bisect_left(procs_ts, ts - 5), bisect.bisect_right(procs_ts, ts + 5)
    bg = {}
    for p in procs[plo:phi]:
        n = (p["name"] or "").lower()
        if p["cpu_pct"] and p["cpu_pct"] > 10 and n != (game_exe or "").lower() and n not in NOT_BACKGROUND:
            bg[p["name"]] = max(bg.get(p["name"], 0), p["cpu_pct"])
    for name in sorted(bg, key=lambda k: -bg[k])[:2]:
        causes.append(f"background:{name}")
    if win and game_exe:
        fg = [s["fg_exe"] for s in win if s["fg_exe"]]
        if fg and any(f.lower() != game_exe.lower() for f in fg):
            causes.append("alt_tab")
    if win:
        if (mx("dpc_pct") or 0) > 5 or (mx("interrupt_pct") or 0) > 5:
            causes.append("dpc")
        if (mx("cpu_temp") or 0) > 90 or (mx("gpu_hotspot") or 0) > 100:
            causes.append("thermal")
    if ext is not None:
        causes = ext.refine(ts, win, causes)
    if not causes:
        causes.append("shader_or_unknown")
    return causes


def session_report(con, session_id):
    row = _one(con, "SELECT * FROM sessions WHERE id=?", (int(session_id),))
    if not row:
        return {"error": f"Brak sesji {session_id}"}
    start = row["start_ts"]
    last_sample = _one(con, "SELECT MAX(ts) AS t FROM samples WHERE game_session=?", (row["id"],))["t"]
    ends = _eff_ends(con, [row])
    end = ends[row["id"]] or last_sample or time.time()
    dur = max(1.0, end - start)
    exe = row["exe"]
    rep = {"session": _flatten_session(row, ends=ends), "summary": _json(row["summary"]) or {}, "duration_s": round(dur),
           "start": fmt_ts(start), "end": fmt_ts(end)}

    # --- frames: bound shares + fps timeline; fps = application frames (frame generation left out), disp_fps presented
    frames = _rows(con, f"SELECT ts, {framestats.APP_FPS} AS fps, fps AS disp_fps, frames, ft_p99, ft_max, cpu_busy, "
                        f"gpu_busy, bound, present_mode, frame_gen_pct FROM frames WHERE session=? ORDER BY ts",
                   (row["id"],))
    bound_counts = Counter(f["bound"] for f in frames if f["bound"])
    nb = sum(bound_counts.values())
    rep["bound_pct"] = {k: _r(100 * bound_counts.get(k, 0) / nb) for k in ("GPU", "CPU", "limit")} if nb else {}
    fps = [f["fps"] for f in frames if f["fps"]]
    if fps:
        avg = sum(fps) / len(fps)
        rep["fps_timeline"] = {
            "seconds": len(frames), "avg": _r(avg), "min": _r(min(fps)), "max": _r(max(fps)),
            "p5": _r(_pct(fps, 0.05)), "p50": _r(_pct(fps, 0.5)), "p95": _r(_pct(fps, 0.95)),
            "seconds_below_60pct_of_avg": sum(1 for v in fps if v < 0.6 * avg),
            "worst_ft_max_ms": _r(max((f["ft_max"] or 0) for f in frames)),
            "present_mode": Counter(f["present_mode"] for f in frames if f["present_mode"]).most_common(1)[0][0]
            if any(f["present_mode"] for f in frames) else None,
        }
        disp = [f["disp_fps"] for f in frames if f["disp_fps"]]
        gen = [(f["frame_gen_pct"], f["frames"] or 0) for f in frames if f["frame_gen_pct"] is not None]
        gw = sum(w for _, w in gen)
        rep["fps_timeline"].update(displayed_avg=_r(sum(disp) / len(disp)) if disp else None,
                                   frame_gen_pct=_r(sum(v * w for v, w in gen) / gw) if gw else None)
    else:
        rep["fps_timeline"] = {}
    summ = rep["summary"]
    rep["pacing"] = summ.get("pacing") or framestats.session_pacing(con, row["id"])

    # --- stutters with causes
    # stutters.causes is never written (causes need the session's baseline): judged here on every report
    st = _rows(con, "SELECT ts, ft_ms, median_ms, side, cpu_busy, cpu_wait, gpu_busy, gpu_wait, generated, anim_err "
                    "FROM stutters WHERE session=? ORDER BY ts", (row["id"],))
    samples = _rows(con, "SELECT ts, cpu_core_max, gpu_load, gpu_vram_mb, gpu_vram_total_mb, ram_pct, commit_gb, pagefile_pct, "
                         "commit_limit_gb, disk_lat_ms, hard_faults_s, dpc_pct, interrupt_pct, cpu_temp, gpu_hotspot, "
                         "fg_exe, gpu_power_w, user_idle_s, cpu_total FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
                    (start - 3, end + 3))
    procs = _rows(con, "SELECT ts, name, cpu_pct FROM procs WHERE kind='cpu' AND ts BETWEEN ? AND ? ORDER BY ts",
                  (start - 6, end + 6))
    s_ts, p_ts = [s["ts"] for s in samples], [p["ts"] for p in procs]
    cause_counts = Counter()
    bg_counts = Counter()
    ext = diagreport.StutterExt(con, row, start, end)
    for s in st:
        cs = _causes_for_stutter(s, s_ts, samples, p_ts, procs, exe, ext)
        s["causes"] = cs
        for c in cs:
            cause_counts[c] += 1
            if c.startswith("background:"):
                bg_counts[c.split(":", 1)[1]] += 1
    ranked, sides = _rank_causes(st, frames, samples, start, s_ts, p_ts, procs, exe, ext)
    if ranked is not None:   # keep only what stutters have more often than normal gameplay
        confirmed = {r["tag"] for r in ranked if r["verdict"] in ("confirmed", "weak")}
        cause_counts = Counter()
        for s in st:
            s["causes"] = [c for c in s["causes"] if c in confirmed] or ["shader_or_unknown"]
            cause_counts.update(s["causes"])
        bg_counts = Counter({k: v for k, v in bg_counts.items() if f"background:{k}" in confirmed})
    worst = sorted(st, key=lambda s: -s["ft_ms"])[:15]
    rep["stutters"] = {
        "count": len(st), "per_min": _r(len(st) / (dur / 60), 2),
        "worst_ft_ms": _r(max((s["ft_ms"] for s in st), default=None)),
        "cause_counts": dict(cause_counts.most_common()),
        "cause_ranking": ranked,
        "ambient": [r for r in ranked or [] if r["verdict"] == "ambient" and r["tag"] != "shader_or_unknown"],
        "background_procs": dict(bg_counts.most_common(5)),
        "worst": [{"ts": w["ts"], "time": fmt_ts(w["ts"]), "ft_ms": _r(w["ft_ms"]), "median_ms": _r(w["median_ms"]),
                   "causes": w["causes"], "side": w["side"], "cpu_busy": _r(w["cpu_busy"]),
                   "gpu_busy": _r(w["gpu_busy"]), "cpu_wait": _r(w["cpu_wait"]), "gpu_wait": _r(w["gpu_wait"]),
                   "generated": w["generated"], "anim_err": _r(w["anim_err"])} for w in worst],
        # where the long frames' time went (PresentMon busy times); NULL side = recorded before the 2.x columns
        "sides": dict(Counter(s["side"] for s in st if s["side"]).most_common()),
        "side_ranking": sides,
    }

    # --- hardware peaks
    agg = _one(con, "SELECT COUNT(*) AS n, MAX(cpu_temp) AS cpu_temp_max, AVG(cpu_temp) AS cpu_temp_avg, "
                    "MAX(gpu_temp) AS gpu_temp_max, MAX(gpu_hotspot) AS gpu_hotspot_max, MAX(gpu_mem_temp) AS gpu_mem_temp_max, "
                    "AVG(cpu_clock_mhz) AS cpu_clock_avg, MAX(cpu_clock_mhz) AS cpu_clock_max, "
                    "AVG(gpu_clock_mhz) AS gpu_clock_avg, MAX(gpu_clock_mhz) AS gpu_clock_max, "
                    "AVG(gpu_load) AS gpu_load_avg, AVG(cpu_total) AS cpu_total_avg, MAX(cpu_core_max) AS cpu_core_max_peak, "
                    "MAX(gpu_vram_mb) AS vram_peak_mb, MAX(gpu_vram_total_mb) AS vram_total_mb, "
                    "MAX(ram_pct) AS ram_peak_pct, MAX(ram_used_gb) AS ram_peak_gb, MAX(commit_gb) AS commit_peak_gb, "
                    "MAX(commit_limit_gb) AS commit_limit_gb, MAX(disk_lat_ms) AS disk_lat_max_ms, "
                    "MAX(gpu_power_w) AS gpu_power_max_w, MAX(cpu_power_w) AS cpu_power_max_w, "
                    "MAX(ui_ms) AS ui_ms_max, MAX(dwm_ms) AS dwm_ms_max, MAX(dpc_pct) AS dpc_max, "
                    "MAX(interrupt_pct) AS interrupt_max "
                    "FROM samples WHERE ts BETWEEN ? AND ?", (start, end))
    rep["hardware"] = {k: _r(v, 1) for k, v in agg.items()} if agg else {}

    # --- events & incidents during the session
    rep["incidents"] = incidents_between(con, start, end + 5)
    rep["events"] = _rows(con, "SELECT ts, provider, event_id, level, substr(message,1,300) AS message FROM events "
                               "WHERE ts BETWEEN ? AND ? AND (level<=2 OR event_id IN (1000,1002,1001,10001)) "
                               "ORDER BY ts LIMIT 60", (start, end + 60))
    for e in rep["events"]:
        e["time"] = fmt_ts(e["ts"])
    # --- network during the session + does it line up with the stutters (network lag vs local stutter)
    nrows = _net_rows(con, start, end)
    rep["net"] = net_summary(con, start, end, rows=nrows)
    rep["net"]["correlation"] = (net_correlation(con, nrows, [s["ts"] for s in st], start, end)
                                 if rep["net"]["seconds"] else None)
    rep["marks"] = marks.report(con, start, end + 5, exe)
    diagreport.extend(con, rep, row, start, end)
    rep["findings"] = _session_findings(rep, row)
    return rep


def _rank_causes(st, frames, samples, start, s_ts, p_ts, procs, exe, ext):
    """(stuttercause.rank of the causes, framestats.side_ranking of the stutter sides) over gameplay seconds;
    None for either when the session has too little gameplay or timing to judge."""
    keep = gameplay.select(frames, samples, start)
    if len(keep) < gameplay.MIN_GAMEPLAY_S:
        return None, None
    secs = {int(f["ts"]) for f in keep}
    played = [s for s in st if int(s["ts"]) in secs]
    if not played:
        return None, None
    base = stuttercause.baseline_ts([f["ts"] for f in keep], [s["ts"] for s in st])
    base_tags = [_causes_for_stutter({"ts": t}, s_ts, samples, p_ts, procs, exe, ext) for t in base]
    bound_at = {int(f["ts"]): f["bound"] for f in keep if f["bound"]}
    return (stuttercause.rank([s["causes"] for s in played], base_tags),
            framestats.side_ranking(played, base, bound_at))


def _session_findings(rep, row):
    f = []
    add = lambda sev, title, detail: f.append({"severity": sev, "title": title, "detail": detail})
    exe = row["exe"]
    fl = rep.get("fps_timeline") or {}
    summ = rep.get("summary") or {}
    bp = rep.get("bound_pct") or {}
    st = rep.get("stutters") or {}
    hw = rep.get("hardware") or {}
    n_st = st.get("count", 0)

    if row["end_reason"] == "crash":
        add("critical", *crashinfo.finding(rep.get("crash"), exe))
    if row["end_reason"] == "unknown":
        add("serious", "Sesja zakończona w nieznany sposób",
            "Kolektor nie zapisał końca sesji - możliwy twardy reset/zawieszenie komputera podczas gry.")
    if not fl:
        add("info", "Brak danych o klatkach",
            "PresentMon nie zapisał klatek (brak PresentMon, brak uprawnień administratora lub bardzo krótka sesja).")
    else:
        if bp:
            top = max(bp, key=lambda k: bp[k] or 0)
            if (bp.get("GPU") or 0) >= 60:
                add("info", f"GPU limituje wydajność przez {bp['GPU']:.0f}% czasu",
                    "Karta jest wąskim gardłem (normalne w grach AAA). Aby podnieść FPS: obniż rozdzielczość lub użyj "
                    "FSR/upscalera, zmniejsz ustawienia cieni/RT/tekstur.")
            if (bp.get("CPU") or 0) >= 40:
                add("warning", f"CPU limituje wydajność przez {bp['CPU']:.0f}% czasu",
                    "Procesor nie nadąża z przygotowaniem klatek. Sprawdź procesy w tle (lista w stutters/background_procs), "
                    "obniż ustawienia mocno obciążające CPU (zasięg widzenia, tłum, symulacja), upewnij się, że EXPO i "
                    "plan zasilania Wysoka wydajność/Zrównoważony z Game Mode działają poprawnie.")
            if (bp.get("limit") or 0) >= 40:
                add("info", f"Brak limitu CPU/GPU przez {bp['limit']:.0f}% czasu (limit FPS / vsync)",
                    "Ani CPU, ani GPU nie pracują pełnym czasem klatki. Sprawdź limiter FPS w grze, V-Sync, Radeon Chill, "
                    "FRTC (Frame Rate Target Control) w Adrenalinie oraz limit odświeżania monitora.")
        # application frames: with frame generation the presented 1% low hides hitches between generated frames
        avg = summ.get("app_fps_avg") or summ.get("fps_avg") or fl.get("avg")
        low1 = summ.get("app_fps_1low") or summ.get("fps_1low")
        if avg and low1 and low1 / avg < 0.6:
            add("warning", f"Nierówny frametime: 1% low {low1} FPS przy średniej {avg} FPS",
                "Duża rozbieżność między średnią a 1% low oznacza przycięcia. Zobacz przyczyny stutterów poniżej.")
        if fl.get("seconds_below_60pct_of_avg", 0) > 0.05 * fl.get("seconds", 1):
            add("info", f"{fl['seconds_below_60pct_of_avg']} s z FPS poniżej 60% średniej",
                "Fragmenty gry z wyraźnymi spadkami - porównaj timestampy w `worst` z wykresem w UI.")

    if n_st:
        rate = st.get("per_min") or 0
        sev = "serious" if rate > 6 else "warning" if rate > 2 else "info"
        add(sev, f"{n_st} stutterów ({rate}/min, najgorszy {st.get('worst_ft_ms')} ms)",
            "Przyczyny: " + ", ".join(f"{k}={v}" for k, v in (st.get("cause_counts") or {}).items()))
        cc = st.get("cause_counts") or {}
        share = lambda k: cc.get(k, 0) / n_st
        if cc.get("disk", 0) >= 3 and share("disk") >= 0.25:
            add("serious", f"{cc['disk']} stutterów powiązanych z dyskiem",
                "Opóźnienia dysku/ładowanie z pliku wymiany w trakcie przycięć. Zainstaluj grę na Samsung 990 PRO "
                "(nie na Goodram), sprawdź plik wymiany (system-managed na szybkim dysku), zamknij skanery/pobieranie w tle.")
        if cc.get("vram", 0) >= 3:
            add("warning", f"{cc['vram']} stutterów przy pełnym VRAM",
                "Pamięć karty >95%. Obniż jakość tekstur / rozdzielczość, wyłącz RT lub ultra tekstury.")
        if cc.get("ram", 0) >= 3:
            add("warning", f"{cc['ram']} stutterów przy dużym zużyciu RAM/commit",
                "RAM >90% lub commit blisko limitu. Zamknij przeglądarkę/inne aplikacje, zwiększ plik wymiany.")
        if cc.get("cpu_thread", 0) >= 3 and share("cpu_thread") >= 0.25:
            add("warning", f"{cc['cpu_thread']} stutterów przy 100% jednego wątku CPU",
                "Gra jest ograniczona jednym wątkiem (GPU niedociążone). Obniż ustawienia CPU-zależne, sprawdź procesy w tle.")
        if st.get("background_procs"):
            names = ", ".join(f"{k} ({v}x)" for k, v in st["background_procs"].items())
            add("warning", "Procesy w tle obciążające CPU podczas stutterów", f"{names}. Zamknij je na czas grania lub wyłącz autostart.")
        if cc.get("alt_tab", 0) >= 1:
            add("info", f"{cc['alt_tab']} stutterów gdy gra nie była na pierwszym planie",
                "Alt-Tab / nakładki / okna na wierzchu mogły wywołać przycięcia.")
        if cc.get("dpc", 0) >= 3:
            add("warning", f"{cc['dpc']} stutterów z wysokim DPC/przerwaniami",
                "Podejrzany sterownik (sieć, audio, USB, RGB/Gigabyte). Zaktualizuj chipset/LAN/audio, wyłącz zbędne usługi producenta.")
        if cc.get("thermal", 0) >= 1:
            add("warning", f"{cc['thermal']} stutterów przy wysokiej temperaturze",
                "CPU >90°C lub hotspot GPU >100°C - poprawa chłodzenia, krzywa wentylatorów, limit mocy/PBO.")
        if cc.get("shader_or_unknown", 0) >= 0.5 * n_st and n_st >= 5:
            add("info", "Większość stutterów bez przyczyny systemowej",
                "Zwykle kompilacja shaderów lub streaming zasobów w grze. Pomaga: wstępna kompilacja shaderów, "
                "aktualne sterowniki Adrenalin, gra na szybkim SSD, ReBAR/SAM włączony.")
        side = next((r for r in st.get("side_ranking") or [] if r["verdict"] == "confirmed"), None)
        if side:
            add("info", f"{side['stutter_pct']:.0f}% stutterów powstaje {framestats.SIDE_LABELS[side['tag']]}",
                {"cpu": "W długich klatkach procesor pracował przez większość czasu klatki, częściej niż w zwykłej grze: "
                        "kompilacja shaderów, streaming, wątek gry, procesy w tle lub DPC.",
                 "gpu": "W długich klatkach karta pracowała przez większość czasu klatki, częściej niż w zwykłej grze: "
                        "skoki obciążenia GPU, przepełniony VRAM lub zegar GPU.",
                 "present": "Ani CPU, ani GPU nie pracowały przez większość długiej klatki: czekała w kolejce prezentacji "
                            "(V-Sync, DWM, sterownik, nakładki, generowanie klatek)."}[side["tag"]])
        amb = st.get("ambient") or []
        if st.get("cause_ranking") is None:
            add("info", "Za mało rozgrywki, by ocenić przyczyny stutterów",
                "Sesja to głównie menu, ładowanie lub kompilacja shaderów. Podane przyczyny są niepotwierdzone.")
        if amb:
            add("info", "Stałe warunki, których nie da się ocenić w tej sesji",
                ", ".join(f"{r['tag']} ({r['base_pct']:.0f}% zwykłych sekund)" for r in amb[:3]) +
                ": występują także bez stutterów. Sprawdź je testem przed/po (pm test start).")

    if hw:
        tc, th = hw.get("cpu_temp_max"), hw.get("gpu_hotspot_max")
        if (tc and tc >= 95) or (th and th >= 105):
            add("serious", "Temperatury na granicy throttlingu", f"CPU max {tc}°C, hotspot GPU max {th}°C. Sprawdź chłodzenie i przepływ powietrza.")
        elif (tc and tc > 90) or (th and th > 100):
            add("warning", "Wysokie temperatury", f"CPU max {tc}°C, hotspot GPU max {th}°C.")
        vt, vp = hw.get("vram_total_mb"), hw.get("vram_peak_mb")
        if vt and vp and vp / vt > 0.95:
            add("warning", f"VRAM wykorzystany w {vp / vt * 100:.0f}% (szczyt {vp:.0f}/{vt:.0f} MB)", "Obniż jakość tekstur lub rozdzielczość.")
        if (hw.get("ram_peak_pct") or 0) > 90:
            add("warning", f"RAM do {hw['ram_peak_pct']}%", "Zamknij aplikacje w tle; sprawdź plik wymiany.")
        if (hw.get("ui_ms_max") or 0) >= 2000 or (hw.get("dwm_ms_max") or 0) >= 2000:
            add("critical", "Zawieszenie pulpitu/DWM podczas sesji",
                f"ui_ms max {hw.get('ui_ms_max')}, dwm_ms max {hw.get('dwm_ms_max')} - szczegóły w incydentach.")
    net = rep.get("net") or {}
    if net.get("seconds"):
        ninc = Counter(i["kind"] for i in net.get("incidents") or [])
        gl = (net.get("targets") or {}).get("game") or {}
        if ninc.get("net_down") or ninc.get("net_link_down"):
            add("serious", f"Utrata połączenia z internetem podczas gry ({ninc['net_down'] + ninc['net_link_down']}x)",
                "Szczegóły i miejsce awarii (dom/operator/internet) w incydentach net_down / net_link_down.")
        if ninc.get("game_lag"):
            add("warning", f"Lag do serwera gry ({ninc['game_lag']}x)",
                "Wzrost pingu lub utrata pakietów do serwera gry - to lag sieciowy, nie przycięcia PC. Pole `where` w "
                "incydencie game_lag wskazuje winowajcę (dom/operator/internet/serwer gry).")
        if ninc.get("packet_loss"):
            add("warning", f"Utrata pakietów podczas gry ({ninc['packet_loss']}x)",
                "; ".join(i["title"] for i in net["incidents"] if i["kind"] == "packet_loss") or "Sprawdź sekcję net.")
        if (gl.get("loss_pct") or 0) >= 1:
            add("warning", f"Utrata pakietów do serwera gry {gl['loss_pct']}%", "Rubber-banding / lag w grze online.")
        cor = net.get("correlation") or {}
        near_pct = cor.get("stutters_near_net_problem_pct") or 0
        if (cor.get("stutters_near_net_problem") or 0) >= 2 and near_pct >= 30 and near_pct > 2 * (cor.get("chance_pct") or 0):
            add("warning", f"{cor['stutters_near_net_problem']} z {cor['stutters']} stutterów zbiega się z problemami sieci",
                f"W oknie +-{cor['window_s']:g} s od skoku pingu / utraty pakietów ({cor['stutters_near_net_problem_pct']}% "
                f"stutterów przy {cor['chance_pct']}% czasu z problemami sieci) - przycięcia mogą być lagiem sieciowym.")
        elif cor.get("bad_seconds") and cor.get("stutters") and near_pct < 30:
            add("info", "Stuttery w większości nie pokrywają się z problemami sieci",
                f"{cor['stutters_near_net_problem']} z {cor['stutters']} stutterów blisko skoku pingu / utraty pakietów - "
                "pozostałe przycięcia są raczej lokalne (PC), nie sieciowe.")
    for inc in rep.get("incidents") or []:
        if inc["kind"] in ("gpu_driver", "whea", "whea_fatal", "whea_corrected", "whea_pcie", "bsod"):
            add("critical", f"{inc['title']} ({fmt_ts(inc['ts'])})", "Błąd sterownika/sprzętu w trakcie gry - nie ignoruj, sprawdź events.")
    f += diagreport.findings(rep, row)
    if not f:
        add("info", "Brak wykrytych problemów", "Sesja wygląda zdrowo.")
    order = {"critical": 0, "serious": 1, "warning": 2, "info": 3}
    f.sort(key=lambda x: order[x["severity"]])
    return f


# --------------------------------------------------------------------------
# incidents / events
# --------------------------------------------------------------------------

def incidents_between(con, t0, t1, kinds=None, limit=500):
    sql = "SELECT id, ts, end_ts, kind, severity, title, details FROM incidents WHERE ts BETWEEN ? AND ?"
    args = [t0, t1]
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    rows = _rows(con, sql + " ORDER BY ts LIMIT ?", args + [limit])
    for r in rows:
        r["details"] = _json(r["details"])
        r["time"] = fmt_ts(r["ts"])
    return rows


def incidents(con, since, kinds=None):
    t0 = parse_time(since) if not isinstance(since, (int, float)) else since
    return incidents_between(con, t0, time.time() + 1, kinds)


def events(con, since, level=None, grep=None, limit=200):
    t0 = parse_time(since)
    sql, args = "SELECT ts, log, provider, event_id, level, message FROM events WHERE ts>=?", [t0]
    if level is not None:
        sql += " AND level<=?"
        args.append(int(level))
    if grep:
        sql += " AND (message LIKE ? OR provider LIKE ?)"
        args += [f"%{grep}%", f"%{grep}%"]
    rows = _rows(con, sql + " ORDER BY ts DESC LIMIT ?", args + [limit])
    for r in rows:
        r["time"] = fmt_ts(r["ts"])
    return rows


# --------------------------------------------------------------------------
# range / moment
# --------------------------------------------------------------------------

def range_report(con, t0, t1):
    t0, t1 = float(t0), float(t1)
    n = con.execute("SELECT COUNT(*) FROM samples WHERE ts BETWEEN ? AND ?", (t0, t1)).fetchone()[0]
    rep = {"from": fmt_ts(t0), "to": fmt_ts(t1), "duration_s": round(t1 - t0), "sample_count": n}
    stats = {}
    if n:
        sel = ",".join(f"AVG({c}) AS a_{c}, MAX({c}) AS m_{c}" for c in KEY_COLS)
        a = _one(con, f"SELECT {sel} FROM samples WHERE ts BETWEEN ? AND ?", (t0, t1))
        step = max(1, math.ceil(n / 40000))
        sub = _rows(con, f"SELECT {','.join(KEY_COLS)} FROM samples WHERE ts BETWEEN ? AND ? AND "
                         f"CAST(ts AS INTEGER) % ? = 0", (t0, t1, step))
        for c in KEY_COLS:
            if a[f"m_{c}"] is None:
                continue
            stats[c] = {"avg": _r(a[f"a_{c}"], 2), "p95": _r(_pct([s[c] for s in sub], 0.95), 2), "max": _r(a[f"m_{c}"], 2)}
        gaps = _rows(con, "SELECT ts, next_ts - ts AS gap FROM (SELECT ts, LEAD(ts) OVER (ORDER BY ts) AS next_ts "
                          "FROM samples WHERE ts BETWEEN ? AND ?) WHERE gap > 10 ORDER BY gap DESC LIMIT 5", (t0, t1))
        rep["data_gaps"] = [{"after": fmt_ts(g["ts"]), "gap_s": round(g["gap"])} for g in gaps]
        fg = _rows(con, "SELECT fg_exe, COUNT(*) AS seconds FROM samples WHERE ts BETWEEN ? AND ? AND fg_exe IS NOT NULL "
                        "GROUP BY fg_exe ORDER BY seconds DESC LIMIT 6", (t0, t1))
        rep["foreground"] = fg
    rep["stats"] = stats
    rep["incidents"] = incidents_between(con, t0, t1)
    rep["freezes"] = [i for i in rep["incidents"] if i["kind"] in ("ui_freeze", "dwm_stall", "app_not_responding")]
    ev = _rows(con, "SELECT ts, provider, event_id, level, substr(message,1,300) AS message FROM events "
                    "WHERE ts BETWEEN ? AND ? AND level<=2 ORDER BY ts LIMIT 100", (t0, t1))
    for e in ev:
        e["time"] = fmt_ts(e["ts"])
    rep["events"] = ev
    rep["marks"] = marks.report(con, t0, t1)
    rep["top_procs"] = {
        "cpu": _rows(con, "SELECT name, ROUND(AVG(cpu_pct),2) AS avg_cpu_pct, ROUND(MAX(cpu_pct),1) AS max_cpu_pct, "
                          "COUNT(*) AS appearances FROM procs WHERE kind='cpu' AND ts BETWEEN ? AND ? "
                          "AND name NOT IN ('System Idle Process','Idle') GROUP BY name ORDER BY SUM(cpu_pct) DESC LIMIT 10",
                     (t0, t1)),
        "io": _rows(con, "SELECT name, ROUND(AVG(io_mbs),2) AS avg_io_mbs, ROUND(MAX(io_mbs),1) AS max_io_mbs, "
                         "COUNT(*) AS appearances FROM procs WHERE kind='io' AND ts BETWEEN ? AND ? GROUP BY name "
                         "ORDER BY SUM(io_mbs) DESC LIMIT 10", (t0, t1)),
        "mem": _rows(con, "SELECT name, ROUND(AVG(mem_mb)) AS avg_mem_mb, ROUND(MAX(mem_mb)) AS max_mem_mb "
                          "FROM procs WHERE kind='mem' AND ts BETWEEN ? AND ? GROUP BY name ORDER BY AVG(mem_mb) DESC LIMIT 8",
                     (t0, t1)),
    }
    # end_ts <= start_ts = interrupted session whose real end is derived from its data (may overlap the range)
    srows = _rows(con, "SELECT * FROM sessions WHERE start_ts<=? AND (COALESCE(end_ts, ?)>=? OR (end_ts<=start_ts "
                       "AND start_ts>=?)) ORDER BY start_ts", (t1, t1, t0, t0 - 86400))
    ends = _eff_ends(con, srows)
    rep["sessions"] = [_flatten_session(r, ends=ends) for r in srows if (ends[r["id"]] or t1) >= t0]
    rep["net"] = net_summary(con, t0, t1)
    return rep


MOMENT_COLS = ["cpu_total", "cpu_core_max", "cpu_clock_mhz", "cpu_temp", "gpu_load", "gpu_clock_mhz", "gpu_temp",
               "gpu_hotspot", "gpu_vram_mb", "ram_pct", "commit_gb", "hard_faults_s", "disk_lat_ms", "disk_lat_disk",
               "disk_busy_pct", "dpc_pct", "interrupt_pct", "ui_ms", "dwm_ms", "fg_exe", "fg_hung", "user_idle_s",
               "game_session"]


def moment(con, ts, window=30):
    ts, window = float(ts), float(window)
    t0, t1 = ts - window, ts + window
    rows = _rows(con, f"SELECT ts, {','.join(MOMENT_COLS)} FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts", (t0, t1))
    for r in rows:
        r["time"] = fmt_ts(r["ts"])
        r["t_rel"] = _r(r["ts"] - ts, 0)
        for k, v in r.items():
            if isinstance(v, float) and k not in ("ts",):
                r[k] = _r(v, 1)
    procs = _rows(con, "SELECT ts, kind, name, pid, ROUND(cpu_pct,1) AS cpu_pct, ROUND(mem_mb) AS mem_mb, "
                       "ROUND(io_mbs,1) AS io_mbs FROM procs WHERE ts BETWEEN ? AND ? ORDER BY ts, kind", (t0, t1))
    for p in procs:
        p["time"] = fmt_ts(p["ts"])
    evs = _rows(con, "SELECT ts, log, provider, event_id, level, substr(message,1,400) AS message FROM events "
                     "WHERE ts BETWEEN ? AND ? ORDER BY ts", (t0, t1))
    for e in evs:
        e["time"] = fmt_ts(e["ts"])
    prev = _one(con, "SELECT ts FROM samples WHERE ts<? ORDER BY ts DESC LIMIT 1", (t0,))
    nxt = _one(con, "SELECT ts FROM samples WHERE ts>? ORDER BY ts LIMIT 1", (t1,))
    nrows = _net_rows(con, t0, t1)
    for r in nrows:
        r["time"] = fmt_ts(r["ts"])
        r["t_rel"] = _r(r["ts"] - ts, 0)
        r["extra"] = _json(r["extra"])
        for k, v in r.items():
            if isinstance(v, float) and k not in ("ts",):
                r[k] = _r(v, 2)
    near = min(nrows, key=lambda r: abs(r["ts"] - ts)) if nrows else None
    net = {"nearest": near if near and abs(near["ts"] - ts) <= max(5.0, window) else None, "samples": nrows[-301:],
           "summary": net_summary(con, t0, t1)}
    return {"at": fmt_ts(ts), "window_s": window, "samples": rows, "procs": procs, "events": evs, "net": net,
            "incidents": incidents_between(con, t0, t1),
            "sample_gap_note": None if rows else "Brak próbek w tym oknie (kolektor nie działał - np. komputer był wyłączony).",
            "nearest_sample_before": fmt_ts(prev["ts"]) if prev else None,
            "nearest_sample_after": fmt_ts(nxt["ts"]) if nxt else None}


# --------------------------------------------------------------------------
# network quality (net_samples, written by perfmon/netmon.py)
# --------------------------------------------------------------------------

NET_TARGETS = ("gw", "isp", "inet", "game")
NET_TARGET_PL = {"gw": "router", "isp": "pierwszy węzeł operatora", "inet": "internet", "game": "serwer gry"}
NET_KINDS = ["net_down", "net_link_down", "packet_loss", "latency_spike", "game_lag", "wifi_weak"]
NET_COLS = ["gw_ms", "gw_loss", "isp_ms", "isp_loss", "inet_ms", "inet_loss", "game_ms", "game_loss", "dns_ms", "link",
            "wifi_signal", "wifi_rssi"]
NET_DOWNLOAD_KBS = 2000  # net_rx_kbs above this during a latency spike = a download is saturating the line
_REASON = re.compile(r"(?<![A-Za-z])Reason=([^;\]\r\n]+)")
_REASON_CODE = re.compile(r"ReasonCode=(\d+)")


def _has_net(con):
    return bool(_table_cols(con, "net_samples"))


def _net_rows(con, t0, t1):
    if not _has_net(con):
        return []
    return _rows(con, f"SELECT ts, session, {','.join(NET_COLS)}, extra FROM net_samples WHERE ts BETWEEN ? AND ? "
                      "ORDER BY ts", (float(t0), float(t1)))


def _ms_stats(vals):
    return {"avg": _r(sum(vals) / len(vals), 1) if vals else None, "p50": _r(_pct(vals, 0.5), 1),
            "p95": _r(_pct(vals, 0.95), 1), "p99": _r(_pct(vals, 0.99), 1), "max": _r(max(vals), 1) if vals else None}


def _net_target(rows, k):
    ms = [r[f"{k}_ms"] for r in rows if r[f"{k}_ms"] is not None]
    losses = [r[f"{k}_loss"] for r in rows if r[f"{k}_loss"] is not None]
    # jitter = mean absolute difference of rtts in consecutive seconds
    diffs, prev = [], None
    for r in rows:
        v = r[f"{k}_ms"]
        if v is not None and prev is not None and r["ts"] - prev[0] <= 2.5:
            diffs.append(abs(v - prev[1]))
        prev = (r["ts"], v) if v is not None else None
    d = _ms_stats(ms)
    d.update(jitter=_r(sum(diffs) / len(diffs), 2) if diffs else None, probes=len(losses),
             loss_pct=_r(100 * sum(losses) / len(losses), 2) if losses else None,
             loss_seconds=sum(1 for v in losses if v > 0))
    return d


def _net_wifi_disconnects(con, t0, t1):
    out = []
    for e in _rows(con, "SELECT ts, message FROM events WHERE provider LIKE '%WLAN-AutoConfig%' AND event_id=8003 "
                        "AND ts BETWEEN ? AND ? ORDER BY ts", (t0, t1)):
        msg = e["message"] or ""
        m, c = _REASON.search(msg), _REASON_CODE.search(msg)
        code = int(c.group(1)) if c else None
        out.append({"ts": e["ts"], "time": fmt_ts(e["ts"]), "reason_code": code,
                    "reason": m.group(1).strip() if m else None, "user_initiated": code == 3})
    return out


def net_summary(con, t0, t1, rows=None):
    """Network quality over a time range: per-target latency/jitter/loss, DNS, link, Wi-Fi, NIC errors, incidents,
    and a short Polish verdict. Always returns the same shape (empty stats when there is no net data)."""
    t0, t1 = float(t0), float(t1)
    rows = _net_rows(con, t0, t1) if rows is None else rows
    rep = {"from": fmt_ts(t0), "to": fmt_ts(t1), "seconds": len(rows), "targets": {}, "dns": None,
           "disconnected_s": 0, "link_s": {}, "wifi": None, "nic_errors": {}, "incidents": [], "verdict": []}
    rep["targets"] = {k: _net_target([], k) for k in NET_TARGETS}
    if _has_net(con):
        rep["incidents"] = incidents_between(con, t0, t1, kinds=NET_KINDS)
    if not rows:
        rep["verdict"] = ["Brak danych sieciowych w tym zakresie (monitor sieci nie działał lub starsza baza)."]
        return rep
    rep["targets"] = {k: _net_target(rows, k) for k in NET_TARGETS}
    dns = [r["dns_ms"] for r in rows if r["dns_ms"] is not None]
    # extra only stores changes: seed the state (SSID/BSSID/channel) that was current when the range started
    extras = [{}]
    seed = _rows(con, "SELECT extra FROM net_samples WHERE ts < ? AND extra IS NOT NULL ORDER BY ts DESC LIMIT 200",
                 (t0,)) if rows[0]["link"] == "wifi" else []
    for r in reversed(seed):
        x = _json(r["extra"])
        if isinstance(x, dict):
            extras[0].update({k: v for k, v in x.items() if k.startswith("wifi_")})
    for r in rows:
        x = _json(r["extra"]) if r["extra"] else None
        extras.append(x if isinstance(x, dict) else {})
    fails = sum(1 for x in extras if x.get("dns_failed"))
    rep["dns"] = {"probes": len(dns) + fails, "avg": _r(sum(dns) / len(dns), 1) if dns else None,
                  "p95": _r(_pct(dns, 0.95), 1), "failures": fails}
    rep["disconnected_s"] = sum(1 for r in rows if r["link"] is None)
    rep["link_s"] = dict(Counter(r["link"] for r in rows if r["link"]))
    nic = Counter()
    for x in extras:
        if isinstance(x.get("nic_errors"), dict):
            for k, v in x["nic_errors"].items():
                nic[k] += v or 0
    rep["nic_errors"] = {**nic, "total": sum(nic.values())} if nic else {}

    sig = [r["wifi_signal"] for r in rows if r["wifi_signal"] is not None]
    rssi = [r["wifi_rssi"] for r in rows if r["wifi_rssi"] is not None]
    bssids = list(dict.fromkeys(x["wifi_bssid"] for x in extras if x.get("wifi_bssid")))
    chans = list(dict.fromkeys(x["wifi_channel"] for x in extras if x.get("wifi_channel") is not None))
    ssids = list(dict.fromkeys(x["wifi_ssid"] for x in extras if x.get("wifi_ssid")))
    disc = _net_wifi_disconnects(con, t0, t1)
    if sig or rssi or disc:
        rep["wifi"] = {
            "seconds": len(sig), "signal_min": min(sig) if sig else None,
            "signal_avg": _r(sum(sig) / len(sig), 1) if sig else None,
            "rssi_min": min(rssi) if rssi else None, "rssi_max": max(rssi) if rssi else None,
            "ssids": ssids, "bssids": bssids, "channels": chans,
            "disconnects": len(disc), "unexpected_disconnects": sum(1 for d in disc if not d["user_initiated"]),
            "disconnect_events": disc}
    rep["verdict"] = _net_verdict(con, rep, t0, t1)
    return rep


def _net_verdict(con, rep, t0, t1):
    v = []
    tg = rep["targets"]
    n = rep["seconds"]
    if rep["disconnected_s"]:
        v.append(f"Brak łącza sieciowego przez {rep['disconnected_s']} s ({100 * rep['disconnected_s'] / n:.1f}% czasu)")
    # loss: report at the closest-to-user hop that already shows it, that is where the problem starts
    # Routers rate-limit ICMP to themselves, so a hop is only blamed when the traffic behind it is lossy too
    end_lossy = [k for k in ("inet", "game") if (tg[k]["loss_pct"] or 0) >= 1]
    lossy = [(k, tg[k]["loss_pct"]) for k in NET_TARGETS if (tg[k]["loss_pct"] or 0) >= 1]
    if end_lossy:
        k, pct = lossy[0]
        where = {"gw": "problem po stronie Wi-Fi/routera (sieci domowej)",
                 "isp": "problem po stronie operatora (łącze/pierwszy węzeł)",
                 "inet": "problem w internecie lub u operatora",
                 "game": "problem na trasie do serwera gry"}[k]
        v.append(f"Utrata pakietów {pct:.1f}% ({NET_TARGET_PL[k]}) - {where}")
    for k in NET_TARGETS:
        t = tg[k]
        if t["probes"] < 20 or t["p50"] is None:
            continue
        if t["p99"] > max(3 * t["p50"], t["p50"] + 40):
            v.append(f"Skoki pingu do: {NET_TARGET_PL[k]} (mediana {t['p50']} ms, p99 {t['p99']} ms, max {t['max']} ms)")
        elif (t["jitter"] or 0) > max(5, 0.5 * t["p50"]):
            v.append(f"Niestabilny ping do: {NET_TARGET_PL[k]} (jitter {t['jitter']} ms przy medianie {t['p50']} ms)")
    gw = tg.get("gw") or {}
    if (gw.get("p95") or 0) > 15 and rep["link_s"].get("wifi"):
        v.append(f"Wysokie opóźnienie do routera przez Wi-Fi (p95 {gw['p95']} ms) - zakłócenia/zasięg, rozważ kabel")
    dns = rep["dns"] or {}
    if dns.get("failures"):
        v.append(f"Błędy DNS: {dns['failures']} (serwer DNS operatora/routera)")
    elif (dns.get("p95") or 0) > 150:
        v.append(f"Wolny DNS (p95 {dns['p95']} ms) - rozważ 1.1.1.1 / 8.8.8.8")
    w = rep["wifi"]
    if w:
        if w["signal_min"] is not None and w["signal_min"] < 40:
            v.append(f"Słaby sygnał Wi-Fi (min {w['signal_min']}%, śr. {w['signal_avg']}%)")
        if w["unexpected_disconnects"]:
            v.append(f"{w['unexpected_disconnects']} nieoczekiwanych rozłączeń Wi-Fi")
        if len(w["bssids"]) > 1:
            v.append(f"Zmiana punktu dostępowego Wi-Fi ({len(w['bssids'])} BSSID) - roaming może dawać przerwy")
    if rep["nic_errors"].get("total"):
        v.append(f"Błędy karty sieciowej: {rep['nic_errors']['total']} (kabel/sterownik)")
    spikes = [i for i in rep["incidents"] if i["kind"] in ("latency_spike", "packet_loss", "game_lag")]
    if spikes and "net_rx_kbs" in _table_cols(con, "samples"):
        heavy = 0
        for i in spikes:
            if i["kind"] != "latency_spike":
                continue
            a = _one(con, "SELECT AVG(net_rx_kbs) AS a FROM samples WHERE ts BETWEEN ? AND ?",
                     (i["ts"], i["end_ts"] or i["ts"] + 30))
            if a and (a["a"] or 0) >= NET_DOWNLOAD_KBS:
                heavy += 1
        if heavy:
            v.append(f"Skoki pingu w trakcie pobierania ({heavy}) - łącze zapchane; ogranicz pobieranie lub włącz QoS/SQM")
    if not v:
        inet = tg.get("inet") or {}
        v.append("Ping stabilny" + (f" (internet: mediana {inet['p50']} ms, p99 {inet['p99']} ms)" if inet.get("p50") is not None else ""))
    return v


def net_series(con, t0, t1, max_points=1500):
    """Chart series like series(): ts + gw/isp/inet/game ms (max per bucket), inet_loss in % (max), wifi_signal (min)."""
    t0, t1 = float(t0), float(t1)
    cols = ["gw_ms", "isp_ms", "inet_ms", "game_ms", "inet_loss", "wifi_signal"]
    if not _has_net(con):
        return {"ts": [], "step_s": 1.0, **{c: [] for c in cols}}
    step = max(1.0, math.ceil((t1 - t0) / max(1, max_points)))
    rows = con.execute("SELECT CAST((ts-?)/? AS INTEGER) AS b, MIN(ts) AS ts, MAX(gw_ms) AS gw_ms, MAX(isp_ms) AS isp_ms, "
                       "MAX(inet_ms) AS inet_ms, MAX(game_ms) AS game_ms, MAX(inet_loss) AS inet_loss, "
                       "MIN(wifi_signal) AS wifi_signal FROM net_samples WHERE ts BETWEEN ? AND ? GROUP BY b ORDER BY b",
                       (t0, step, t0, t1)).fetchall()
    out = {"ts": [r["ts"] for r in rows], "step_s": step}
    for c in cols:
        if c == "inet_loss":
            out[c] = [_r(100 * r[c], 1) if r[c] is not None else None for r in rows]
        else:
            out[c] = [_r(r[c], 2) for r in rows]
    return out


def _net_bad_seconds(rows):
    """Timestamps of seconds with packet loss or a latency spike on any target (spike = well above that target's median)."""
    med = {k: _pct([r[f"{k}_ms"] for r in rows if r[f"{k}_ms"] is not None], 0.5) for k in NET_TARGETS}
    bad = []
    for r in rows:
        for k in NET_TARGETS:
            ms, loss, m = r[f"{k}_ms"], r[f"{k}_loss"], med[k]
            if (loss or 0) > 0 or (ms is not None and m is not None and ms > max(2 * m, m + 30)):
                bad.append(r["ts"])
                break
    return bad


def net_correlation(con, rows, stutter_ts, t0, t1, window=2.0):
    """How many stutters fall within +-window s of a lossy / spiking network second (network lag vs local stutter)."""
    bad = _net_bad_seconds(rows)
    near = sum(1 for t in stutter_ts if bad and bisect.bisect_right(bad, t + window) - bisect.bisect_left(bad, t - window) > 0)
    covered = len({int(b + d) for b in bad for d in range(-int(window), int(window) + 1)})
    return {"bad_seconds": len(bad), "stutters": len(stutter_ts), "stutters_near_net_problem": near,
            "stutters_near_net_problem_pct": _r(100 * near / len(stutter_ts)) if stutter_ts else None,
            "chance_pct": _r(100 * min(covered, max(1, t1 - t0)) / max(1.0, t1 - t0)),
            "window_s": window}


# --------------------------------------------------------------------------
# health / diagnosis
# --------------------------------------------------------------------------

_KV = re.compile(r"(\w+)=([^;\]]*)")


def parse_kernel_power_41(message):
    """Extract the structured fields appended to the message as [Name=val; ...]."""
    m = re.search(r"\[([^\]]*BugcheckCode[^\]]*)\]", message or "")
    if not m:
        return {}
    d = {k: v.strip() for k, v in _KV.findall(m.group(1))}
    out = {"raw": d}
    try:
        code = int(d.get("BugcheckCode", "0"), 0)
    except ValueError:
        code = None
    sleep = d.get("SleepInProgress")
    pb = d.get("PowerButtonTimestamp", "0")
    out["bugcheck_code"] = code
    out["sleep_in_progress"] = sleep
    out["power_button_ts"] = pb
    notes = []
    if code == 0:
        notes.append("BugcheckCode=0: brak BSOD - zasilanie ucięte/twardy reset/natychmiastowy restart bez zrzutu "
                     "(zawieszenie sprzętu, zasilacz/VRM, niestabilny RAM/EXPO, ręczny reset)")
    elif code:
        notes.append(f"BugcheckCode=0x{code:X}: był blue screen")
    if sleep not in (None, "0"):
        notes.append(f"SleepInProgress={sleep}: zdarzyło się w trakcie przejścia w uśpienie/hibernację/zamykanie systemu")
    if pb not in (None, "0", "") and not re.fullmatch(r"0+", pb):
        notes.append("PowerButtonTimestamp != 0: system zarejestrował wciśnięcie przycisku zasilania (użytkownik wyłączył "
                     "zawieszony komputer długim przytrzymaniem?)")
    out["interpretation"] = notes
    out["during_shutdown"] = sleep not in (None, "0", "")
    return out


def _unclean_shutdowns(con, t0):
    rows = _rows(con, "SELECT id, ts, kind, title, details FROM incidents WHERE kind='unclean_shutdown' AND ts>=? ORDER BY ts", (t0,))
    out = []
    for r in rows:
        det = _json(r["details"]) or {}
        msg = det.get("message", "") if isinstance(det, dict) else ""
        info = {"id": r["id"], "ts": r["ts"], "time": fmt_ts(r["ts"]), "source": f"{det.get('provider')} {det.get('event_id')}"
                if isinstance(det, dict) else None}
        if msg:
            info["message"] = msg[:300]
        info.update(parse_kernel_power_41(msg))
        # data before the event: when did we last see samples, what happened right before
        last = _one(con, "SELECT ts, cpu_temp, gpu_hotspot, gpu_load, ram_pct, fg_exe, ui_ms, dwm_ms FROM samples "
                         "WHERE ts<? AND ts>? ORDER BY ts DESC LIMIT 1", (r["ts"], r["ts"] - 6 * 3600))
        if last:
            last = {k: (_r(v) if isinstance(v, float) else v) for k, v in last.items()}
            last["time"] = fmt_ts(last["ts"])
            info["last_sample_before"] = last
            from .ocanalyze import reset_context   # local: ocanalyze imports this module
            ctx = reset_context(con, r["ts"])
            if ctx:
                info["low_load"] = ctx["low_load"]
                if ctx["low_load"]:
                    info.setdefault("interpretation", []).append(
                        f"Reset przy niskim obciążeniu (CPU śr. {ctx['cpu_avg']}% przed resetem) - typowy objaw za "
                        "agresywnego Curve Optimizera na biegu jałowym (jeśli był włączony UV/OC)")
        out.append(info)
    # Kernel-Power 41 and EventLog 6008 usually both exist for the same crash; merge entries within 10 min
    merged = []
    for u in out:
        if merged and u["ts"] - merged[-1]["ts"] < 600:
            if "bugcheck_code" in u and "bugcheck_code" not in merged[-1]:
                u["also"] = merged[-1].get("source")
                merged[-1] = u
            continue
        merged.append(u)
    return merged


def _freezes(con, items):
    """Freeze incidents; if never closed (end_ts NULL) and a later reboot/unclean_shutdown exists, it lasted until that reset."""
    resets = [r["ts"] for r in con.execute(
        "SELECT ts FROM incidents WHERE kind IN ('reboot','unclean_shutdown') AND ts>=? ORDER BY ts",
        (min((f["ts"] for f in items), default=0),))]
    out = []
    for f in items:
        x = {"time": f["time"], "ts": f["ts"], "kind": f["kind"], "title": f["title"],
             "duration_s": _r(f["end_ts"] - f["ts"]) if f["end_ts"] else None, "until_reset": False}
        if not f["end_ts"]:
            i = bisect.bisect_right(resets, f["ts"])
            # only trust a reset within 6 h that follows with no other freeze incident in between
            if i < len(resets) and resets[i] - f["ts"] <= 6 * 3600 and not any(
                    g["ts"] > f["ts"] and g["ts"] < resets[i] for g in items):
                x["until_reset"], x["until_ts"], x["duration_s"] = True, resets[i], _r(resets[i] - f["ts"])
        out.append(x)
    return out


def health_overview(con, days=14):
    t0 = time.time() - float(days) * 86400
    by_kind = {r["kind"]: r["n"] for r in con.execute(
        "SELECT kind, COUNT(*) AS n FROM incidents WHERE ts>=? GROUP BY kind ORDER BY n DESC", (t0,))}
    per_day = defaultdict(dict)
    for r in con.execute("SELECT date(ts,'unixepoch','localtime') AS d, kind, COUNT(*) AS n FROM incidents "
                         "WHERE ts>=? GROUP BY d, kind", (t0,)):
        per_day[r["d"]][r["kind"]] = r["n"]
    unclean = _unclean_shutdowns(con, t0)
    crashes = _rows(con, "SELECT kind, title, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM incidents "
                         "WHERE kind IN ('app_crash','app_hang') AND ts>=? GROUP BY kind, title ORDER BY n DESC LIMIT 15", (t0,))
    for c in crashes:
        c["first"], c["last"] = fmt_ts(c.pop("first_ts")), fmt_ts(c.pop("last_ts"))
        c["app"] = c["title"].split(":", 1)[-1].strip()
    crashinfo.annotate_recurring(con, crashes, t0)
    cfg = incidents_between(con, t0, time.time(), kinds="config_change")
    for c in cfg:
        d = c.get("details")
        c["changed"] = list(d.keys()) if isinstance(d, dict) else None
    freezes = incidents_between(con, t0, time.time(), kinds=["ui_freeze", "dwm_stall", "app_not_responding"])
    cov = _one(con, "SELECT MIN(ts) AS first, MAX(ts) AS last, COUNT(*) AS n FROM samples")
    rep = {
        "days": days, "since": fmt_ts(t0), "incidents_by_kind": by_kind,
        "incidents_per_day": {d: per_day[d] for d in sorted(per_day)},
        "unclean_shutdowns": unclean, "recurring_crashes": crashes, "config_changes": cfg,
        "freezes": _freezes(con, freezes),
        "sample_coverage": {"first": fmt_ts(cov["first"]), "last": fmt_ts(cov["last"]), "samples": cov["n"]} if cov and cov["n"] else None,
    }
    rep["diag_incidents"] = diagreport.health_incidents(con, t0)
    rep["findings"] = diagnose(rep, con, t0)
    return rep


def diagnose(rep, con=None, t0=None):
    """Pattern-based findings across the health overview (Polish)."""
    f = []
    add = lambda sev, title, detail: f.append({"severity": sev, "title": title, "detail": detail})
    un = rep["unclean_shutdowns"]
    bk = rep["incidents_by_kind"]
    if un:
        no_bsod = [u for u in un if u.get("bugcheck_code") == 0]
        shut = [u for u in un if u.get("during_shutdown")]
        pb = [u for u in un if u.get("power_button_ts") not in (None, "0", "") and not re.fullmatch(r"0+", u.get("power_button_ts", "0"))]
        sev = "critical" if len(un) >= 3 else "serious"
        add(sev, f"{len(un)} nieoczekiwanych wyłączeń/twardych resetów w ostatnich {rep['days']} dniach",
            f"W tym {len(no_bsod)} bez BSOD (BugcheckCode=0), {len(shut)} w trakcie zamykania/uśpienia, "
            f"{len(pb)} z wciśniętym przyciskiem zasilania. Ostatni: {un[-1]['time']}.")
        if len(no_bsod) >= 2:
            add("critical", "Powtarzające się resety bez blue screena",
                "BugcheckCode=0 oznacza, że Windows nie zdążył zapisać błędu: kandydaci to zawieszenie GPU/sterownika "
                "(potem ręczny reset), niestabilny RAM (EXPO 5200 na B650 - spróbuj niższego taktowania/większych napięć SoC), "
                "zasilacz, VRM/płyta, przegrzanie. Kroki: wyłącz EXPO na test, zaktualizuj BIOS (AGESA) i sterowniki Adrenalin/chipset, "
                "wyłącz iGPU, usuń Gigabyte EasyTune/AORUS LCD, przetestuj RAM (MemTest86), sprawdź zasilacz.")
        if len(shut) >= 2 and len(shut) >= 0.4 * len(un):
            add("serious", "Wiele resetów podczas zamykania/hibernacji",
                "Problem występuje przy przejściu stanu zasilania: wyłącz Szybkie uruchamianie (Fast Startup), "
                "sprawdź usługi/aplikacje blokujące zamknięcie (steam.exe, Gigabyte), zaktualizuj BIOS i sterowniki chipsetu, "
                "sprawdź zdarzenia 1074/10001 tuż przed resetem.")
    fz = rep["freezes"]
    if fz:
        kinds = Counter(x["kind"] for x in fz)
        add("serious", f"{len(fz)} zawieszeń pulpitu/aplikacji zarejestrowanych przez kolektor",
            ", ".join(f"{k}={v}" for k, v in kinds.items()) + ". Użyj `pm at <czas>` żeby zobaczyć stan systemu tuż przed. "
            "Jeśli system dalej żyje (zamykanie przyciskiem działało) to problem w trybie użytkownika/stosie grafiki "
            "(sterownik GPU, DWM, nakładki), nie w jądrze.")
    for c in rep["recurring_crashes"]:
        if c["n"] >= 3:
            add("warning", f"{c['app']} {('crashuje' if c['kind'] == 'app_crash' else 'wiesza się')} {c['n']}x",
                f"Od {c['first']} do {c['last']}. " + (
                    "Usługa Gigabyte/AORUS - rozważ odinstalowanie (GCC/Control Center, AORUS LCD)." if re.search(r"aorus|gigabyte|gcc|lcd", c["app"], re.I)
                    else f"Moduł {c['module']}, kod {c['code']}: {c['verdict']}." if c.get("verdict")
                    else "Zaktualizuj/przeinstaluj aplikację; sprawdź WER (zdarzenie 1001) po moduł powodujący błąd."))
    net_titles = {"net_down": "brak internetu", "net_link_down": "brak łącza (Wi-Fi/kabel rozłączony)",
                  "packet_loss": "utrata pakietów", "latency_spike": "skok pingu", "game_lag": "lag do serwera gry",
                  "wifi_weak": "słaby sygnał Wi-Fi"}
    nets = {k: bk[k] for k in net_titles if bk.get(k)}
    if nets:
        where = Counter()
        if con is not None and t0 is not None:
            for i in incidents_between(con, t0, time.time(), kinds=list(nets)):
                w = i["details"].get("where") if isinstance(i["details"], dict) else None
                if w:
                    where[w] += 1
        loc = {"home": "dom (Wi-Fi/router)", "isp": "operator", "internet": "internet", "game": "serwer gry"}
        add("serious" if nets.get("net_down") or nets.get("net_link_down") else "warning",
            "Problemy sieciowe: " + ", ".join(f"{net_titles[k]} {v}x" for k, v in nets.items()),
            ("Miejsce problemu: " + ", ".join(f"{loc.get(w, w)} {n}x" for w, n in where.most_common()) + ". " if where else "") +
            "Szczegóły: `pm net --since " + str(rep["days"]) + "d`.")
    for k, name, desc in (
            ("whea", "błąd sprzętowy WHEA", "Błędy sprzętowe WHEA - niestabilny CPU/RAM/PCIe; wyłącz EXPO/PBO na test, zaktualizuj BIOS."),
            ("whea_fatal", "krytyczny błąd CPU (WHEA 18)", "Niestabilny CPU/SoC - cofnij ostatnią zmianę UV/OC (`pm oc stability`)."),
            ("whea_corrected", "skorygowany błąd CPU (WHEA 19)", "Rdzeń na granicy stabilności - zwykle za mocny Curve Optimizer na tym rdzeniu (`pm oc stability`)."),
            ("whea_pcie", "błąd PCIe (WHEA 17)", "Błędy łącza PCIe - sprawdź osadzenie karty, tryb PCIe w BIOS, riser."),
            ("gpu_driver", "problem sterownika GPU", "Resety/timeouty sterownika GPU - czysta instalacja Adrenalin (DDU), wyłącz iGPU, sprawdź zasilanie karty."),
            ("bsod", "blue screen", "Blue screeny - sprawdź kod w zdarzeniach BugCheck."),
            ("disk", "błąd dysku", "Błędy dysku - sprawdź SMART (CrystalDiskInfo) i kable/slot."),
            ("low_memory", "brak pamięci wirtualnej", "Kończyła się pamięć wirtualna - zwiększ plik wymiany/zamknij aplikacje."),
            ("throttle", "throttling CPU", "Throttling CPU - sprawdź chłodzenie i zasilanie VRM.")):
        if bk.get(k):
            add("critical" if k in ("whea", "whea_fatal", "bsod") else
                "serious" if k in ("gpu_driver", "disk", "low_memory", "whea_corrected", "whea_pcie") else "warning",
                f"{bk[k]}x {name}", desc)
    cfg = rep["config_changes"]
    if cfg and un:
        first_bad = min(u["ts"] for u in un)
        near = [c for c in cfg if 0 <= first_bad - c["ts"] <= 3 * 86400]
        if near:
            add("info", "Zmiana konfiguracji tuż przed pierwszym resetem",
                "; ".join(f"{c['time']}: {', '.join(c.get('changed') or [])}" for c in near[:3]) +
                ". Porównaj z `pm sysinfo --diff` (sterowniki/BIOS/ustawienia).")
    if con is not None:
        f += diagreport.health_findings(con, rep)
    if not f:
        f.append({"severity": "info", "title": "Brak istotnych incydentów", "detail": f"W ostatnich {rep['days']} dniach nic niepokojącego."})
    order = {"critical": 0, "serious": 1, "warning": 2, "info": 3}
    f.sort(key=lambda x: order[x["severity"]])
    return f


# --------------------------------------------------------------------------
# comparisons across periods / drivers / game versions
# --------------------------------------------------------------------------

def _session_metrics(con, exe=None, since=None):
    """One dict per finished-or-running session with fps/stutter/bound metrics (oldest first)."""
    t0 = parse_time(since) if since is not None else 0
    sql, args = f"SELECT * FROM sessions WHERE start_ts>=? AND {GAMES_ONLY}", [t0]
    if exe:
        sql += " AND lower(exe)=lower(?)"
        args.append(exe)
    rows = _rows(con, sql + " ORDER BY start_ts", args)
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    q = ",".join("?" * len(ids))
    # application frames: comparing a session with frame generation to one without must not double its FPS
    af, afps = framestats.APP_FRAMES, framestats.APP_FPS
    fr = {r["session"]: r for r in con.execute(
        f"SELECT session, SUM({af}) AS nf, SUM({af}/{afps}) AS secs, COUNT(*) AS n, "
        f"SUM(bound='GPU') AS g, SUM(bound='CPU') AS c, SUM(bound='limit') AS l, "
        f"AVG({framestats.APP_FT_P99}) AS p99, SUM(frames) AS dnf, SUM(frames/fps) AS dsecs, "
        f"SUM(frame_gen_pct*frames)/SUM(CASE WHEN frame_gen_pct IS NOT NULL THEN frames END) AS gen "
        f"FROM frames WHERE {afps}>0 AND session IN ({q}) GROUP BY session", ids)}
    st = {r["session"]: r["n"] for r in con.execute(
        f"SELECT session, COUNT(*) AS n FROM stutters WHERE session IN ({q}) GROUP BY session", ids)}
    out = []
    ends = _eff_ends(con, rows)
    ext = diagreport.session_extras(con, rows)
    for r in rows:
        f = _flatten_session(r, ends=ends)
        end = ends[r["id"]] or time.time()
        minutes = max(0.0, (end - r["start_ts"]) / 60)
        x = fr.get(r["id"])
        fps = (x["nf"] / x["secs"]) if x and x["secs"] else f.get("app_fps_avg") or f.get("fps_avg")
        fps_disp = (x["dnf"] / x["dsecs"]) if x and x["dsecs"] else f.get("fps_avg")
        gen = f.get("frame_gen_pct") if f.get("frame_gen_pct") is not None else x["gen"] if x else None
        low1 = f.get("app_fps_1low")
        if low1 is None:   # summary from before app frames: its 1% low counts generated frames
            low1 = 1000 / x["p99"] if gen and x and x["p99"] else f.get("fps_1low")
        if low1 is None and x and x["p99"]:
            low1 = 1000 / x["p99"]
        ctx = _ctx(r)
        summ = _json(r["summary"]) or {}
        gp = summ.get("gameplay")
        stutters = st.get(r["id"], 0)
        pacing = summ.get("pacing")
        if gp and gp.get("seconds"):   # menus and loading screens do not count
            fps, low1, stutters, minutes = gp["fps_avg"], gp["fps_1low"], gp["stutters"], gp["seconds"] / 60
            if "fps_displayed_avg" in gp:   # stored since frame generation is told apart
                fps_disp, gen, pacing = gp["fps_displayed_avg"], gp["frame_gen_pct"], gp["pacing"]
        bn = summ.get("bottleneck") or {}
        out.append({
            "id": r["id"], "exe": r["exe"], "start_ts": r["start_ts"], "end_ts": f["end_ts"], "minutes": _r(minutes),
            # fps_*: application frames; fps_displayed_avg what the screen showed (higher with frame generation)
            "fps_avg": _r(fps), "fps_1low": _r(low1),
            "fps_01low": f.get("app_fps_01low") or (None if gen else f.get("fps_01low")),
            "fps_displayed_avg": _r(fps_disp), "frame_gen_pct": _r(gen),
            "anim_err_ms": (pacing or {}).get("anim_err_ms"), "disp_jitter_pct": (pacing or {}).get("disp_jitter_pct"),
            "stutters": stutters, "stutters_per_min": _r(stutters / minutes, 2) if minutes > 0.5 else None,
            "usable": gp.get("usable", True) if gp else True,
            "gpu_driver": _driver(ctx), "game_version": ctx.get("game_version"),
            "steam_buildid": (ctx.get("steam") or {}).get("buildid"), "ctx": ctx,
            # rows recorded before the PresentMon 2.x columns were parsed have no `bound`: shares from summary.bottleneck
            "_g": (x["g"] or 0 if x else 0) or (bn.get("gpu_bound_pct") or 0) * minutes,
            "_c": (x["c"] or 0 if x else 0) or (bn.get("cpu_bound_pct") or 0) * minutes,
            "_l": (x["l"] or 0 if x else 0) or (bn.get("limit_pct") or 0) * minutes,
            "_n": x["n"] if x else 0,
            "_secs": x["secs"] if x else 0, **ext.get(r["id"], {}),
        })
    return out


def session_points(con, exe=None, since=None):
    return [{"id": s["id"], "exe": s["exe"], "start_ts": s["start_ts"], "time": fmt_ts(s["start_ts"]),
             "minutes": s["minutes"], "fps_avg": s["fps_avg"], "fps_1low": s["fps_1low"], "fps_01low": s["fps_01low"],
             "stutters_per_min": s["stutters_per_min"], "gpu_driver": s["gpu_driver"], "game_version": s["game_version"]}
            for s in _session_metrics(con, exe, since)]


SMALL_SAMPLE_MIN, SMALL_SAMPLE_SESSIONS = 30, 3   # before/after groups below this: weak evidence


def resolve_point(con, s, exe=None):
    """Split point for before/after: a time (parse_time) or a change marker number 'm3' / '#3' (pm markers)."""
    m = re.fullmatch(r"\s*(?:#|m|marker:?)(\d+)\s*", str(s), re.I)
    if not m:
        return parse_time(s)
    n = int(m.group(1))
    for mk in timeline_markers(con, exe):
        if mk.get("n") == n:
            return mk["ts"]
    raise ValueError(f"Brak markera {n} (lista: pm markers{' --exe ' + exe if exe else ''})")


def compare(con, exe=None, group_by="week", since=None, before=None, after=None):
    """Aggregate sessions per period/driver/game version; delta % is vs the previous group of the same exe.

    before/after (time or marker 'm3'): two groups, sessions started before `before` and at/after `after`
    (default: the same point); sessions in between are left out."""
    if group_by not in ("week", "day", "month", "driver", "game_version", "session", "present_mode"):
        raise ValueError("group_by must be week|day|month|driver|game_version|session|present_mode")
    split = None
    before = before if before is not None else after
    if before is not None:
        b = resolve_point(con, before, exe)
        split = (b, resolve_point(con, after, exe) if after is not None else b)
    groups = {}
    metrics = [s for s in _session_metrics(con, exe, None if split else since) if s["usable"]]
    diagreport.fill_bottleneck(con, metrics)
    for s in metrics:
        if split:
            key = "przed" if s["start_ts"] < split[0] else "po" if s["start_ts"] >= split[1] else None
            if key is None:
                continue
        else:
            d = datetime.fromtimestamp(s["start_ts"])
            key = {"week": d.strftime("%G-W%V"), "day": d.strftime("%Y-%m-%d"), "month": d.strftime("%Y-%m"),
                   "driver": s["gpu_driver"] or "?", "game_version": s["game_version"] or "?",
                   "session": f"#{s['id']}", "present_mode": s.get("present") or "?"}[group_by]
        groups.setdefault((s["exe"], key), []).append(s)
    rows = []
    for (e, key), ss in groups.items():
        mins = sum(s["minutes"] or 0 for s in ss)
        secs = sum(s["_secs"] for s in ss)
        nf = sum(s["fps_avg"] * s["_secs"] for s in ss if s["fps_avg"] and s["_secs"])
        fps = nf / secs if secs else None
        lw = [(s["fps_1low"], s["minutes"] or 0) for s in ss if s["fps_1low"]]
        low1 = sum(v * w for v, w in lw) / sum(w for _, w in lw) if lw and sum(w for _, w in lw) else None

        def mavg(k):   # minutes-weighted over the sessions that have the value
            vs = [(s[k], s["minutes"] or 0) for s in ss if s.get(k) is not None]
            w = sum(m for _, m in vs)
            return _r(sum(v * m for v, m in vs) / w, 2) if w else None
        nb = sum(s["_g"] + s["_c"] + s["_l"] for s in ss)
        rows.append({
            "exe": e, "group": key, "from": min(s["start_ts"] for s in ss),
            "to": max((s["end_ts"] or s["start_ts"]) for s in ss),
            "sessions": len(ss), "minutes": _r(mins), "fps_avg": _r(fps), "fps_1low": _r(low1),
            "fps_displayed_avg": mavg("fps_displayed_avg"), "frame_gen_pct": mavg("frame_gen_pct"),
            "anim_err_ms": mavg("anim_err_ms"), "disp_jitter_pct": mavg("disp_jitter_pct"),
            "stutters_per_min": _r(sum(s["stutters"] for s in ss) / mins, 2) if mins > 0.5 else None,
            "gpu_bound_pct": _r(100 * sum(s["_g"] for s in ss) / nb) if nb else None,
            "cpu_bound_pct": _r(100 * sum(s["_c"] for s in ss) / nb) if nb else None,
            "limit_pct": _r(100 * sum(s["_l"] for s in ss) / nb) if nb else None,
            "bottleneck": _shares([(s.get("bottleneck"), s["minutes"] or 0) for s in ss]),
            "drivers": sorted({s["gpu_driver"] for s in ss if s["gpu_driver"]}),
            "game_versions": sorted({s["game_version"] for s in ss if s["game_version"]}),
            "session_ids": [s["id"] for s in ss],
        })
    rows.sort(key=lambda r: (r["exe"] or "", r["from"]))
    prev = {}
    for r in rows:
        p = prev.get(r["exe"])
        for k in ("fps_avg", "fps_1low", "stutters_per_min"):
            r[f"{k}_delta_pct"] = _r(100 * (r[k] - p[k]) / p[k]) if p and p.get(k) and r.get(k) is not None else None
        prev[r["exe"]] = r
        r["from_time"], r["to_time"] = fmt_ts(r["from"]), fmt_ts(r["to"])
    if split:
        small = {r["exe"] for r in rows if (r["minutes"] or 0) < SMALL_SAMPLE_MIN or r["sessions"] < SMALL_SAMPLE_SESSIONS}
        small |= {e for e in {r["exe"] for r in rows} if sum(1 for r in rows if r["exe"] == e) < 2}
        for r in rows:
            r["small_sample"] = r["exe"] in small
    return rows


def _shares(items):
    """[(key, weight)] -> {key: % of the total weight}, keys without a value left out."""
    tot = sum(w for k, w in items if k)
    acc = Counter()
    for k, w in items:
        if k:
            acc[k] += w
    return {k: _r(100 * w / tot) for k, w in acc.most_common()} if tot else {}


def timeline_markers(con, exe=None, since=None, diag=False):
    """Changes (driver / game update / OS / BIOS / config) detected between consecutive sessions, numbered `n`
    over the whole history (stable whatever `since` is; `pm compare --before m<n>`).

    diag: also user marks and Defender scans (chart markers, not numbered)."""
    since_ts = parse_time(since) if since is not None else 0
    t0 = 0
    ss = _session_metrics(con, None, None)   # full history so the first in-range change is detected
    markers = []

    def walk(items, field, kind, label):
        last = None
        for s in items:
            v = field(s)
            if v and last is not None and v != last and s["start_ts"] >= t0:
                markers.append({"ts": s["start_ts"], "kind": kind, "label": label(last, v, s), "session": s["id"]})
            if v:
                last = v

    walk(ss, lambda s: _driver(s["ctx"]), "driver", lambda a, b, s: f"Sterownik GPU {a} -> {b}")
    walk(ss, lambda s: s["ctx"].get("os_build"), "os", lambda a, b, s: f"Windows {a} -> {b}")
    walk(ss, lambda s: s["ctx"].get("bios"), "bios", lambda a, b, s: f"BIOS {a} -> {b}")
    for e in {s["exe"] for s in ss if not exe or (s["exe"] or "").lower() == exe.lower()}:
        mine = [s for s in ss if s["exe"] == e]
        vers, builds = [], []   # (prev session, session, old, new[, ts]) changes of exe version / Steam buildid
        lv = lb = prev = None
        for s in mine:
            v = s["ctx"].get("game_version")
            st = s["ctx"].get("steam") or {}
            bid = st.get("buildid")
            if v and lv is not None and v != lv:
                vers.append((prev, s, lv, v))
            if bid and lb is not None and bid != lb:
                builds.append((prev, s, lb, bid, st.get("last_updated") or s["start_ts"]))
            lv, lb, prev = v or lv, bid or lb, s
        used = set()
        for p, s, a, b in vers:
            label, ts = f"{e}: wersja {a} -> {b}", s["start_ts"]
            # same update if the build was first seen in this session, or its update time falls in the gap before it
            for i, (bp, bs, ba, bb, bts) in enumerate(builds):
                if i not in used and (bs["id"] == s["id"] or (p and p["start_ts"] <= bts <= s["start_ts"])):
                    used.add(i)
                    label, ts = f"{label} (Steam build {ba} -> {bb})", min(ts, bts)
                    break
            if ts >= t0:
                markers.append({"ts": ts, "kind": "game_update", "label": label, "session": s["id"]})
        for i, (bp, bs, ba, bb, bts) in enumerate(builds):
            if i not in used and bts >= t0:
                markers.append({"ts": bts, "kind": "game_update", "label": f"{e}: Steam build {ba} -> {bb}", "session": bs["id"]})
    for i in incidents_between(con, t0, time.time(), kinds="config_change", limit=100000):
        markers.append({"ts": i["ts"], "kind": "config", "label": i["title"], "session": None})
    markers.sort(key=lambda m: m["ts"])
    for n, m in enumerate(markers, 1):
        m["n"] = n
    markers = [m for m in markers if m["ts"] >= since_ts]
    if diag:
        markers += diagreport.diag_markers(con, since_ts, time.time())
        markers.sort(key=lambda m: m["ts"])
    for m in markers:
        m["time"] = fmt_ts(m["ts"])
    return markers


# --------------------------------------------------------------------------
# time series for charts
# --------------------------------------------------------------------------

def series(con, t0, t1, cols, max_points=1500):
    t0, t1 = float(t0), float(t1)
    valid = set(_table_cols(con, "samples")) - TEXT_SAMPLE_COLS - {"ts"}
    cols = [c for c in cols if c in valid]
    if not cols:
        return {"ts": []}
    step = max(1.0, math.ceil((t1 - t0) / max(1, max_points)))
    sel = ",".join(f"{'MAX' if c in MAX_AGG_SAMPLE else 'AVG'}({c}) AS {c}" for c in cols)
    rows = con.execute(f"SELECT CAST((ts-?)/? AS INTEGER) AS b, MIN(ts) AS ts, {sel} FROM samples "
                       f"WHERE ts BETWEEN ? AND ? GROUP BY b ORDER BY b", (t0, step, t0, t1)).fetchall()
    out = {"ts": [r["ts"] for r in rows], "step_s": step}
    for c in cols:
        out[c] = [_r(r[c], 2) for r in rows]
    return out


def frame_series(con, t0, t1, session=None, max_points=1500):
    t0, t1 = float(t0), float(t1)
    step = max(1.0, math.ceil((t1 - t0) / max(1, max_points)))
    sel = ",".join(f"{expr} AS {name}" for name, expr in FRAME_AGG.items())
    sql = (f"SELECT CAST((ts-?)/? AS INTEGER) AS b, MIN(ts) AS ts, {sel} FROM frames WHERE ts BETWEEN ? AND ?")
    args = [t0, step, t0, t1]
    if session is not None:
        sql += " AND session=?"
        args.append(int(session))
    rows = con.execute(sql + " GROUP BY b ORDER BY b", args).fetchall()
    out = {"ts": [r["ts"] for r in rows], "step_s": step}
    for name in FRAME_AGG:
        out[name] = [_r(r[name], 2) for r in rows]
    return out
