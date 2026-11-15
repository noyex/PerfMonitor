"""View models of the window: the home screen, the game tiles, one game, the problems list.

The window shows what a person can act on; everything else stays in the database for the user's AI agent (`pm`)."""
import re
import time

from perfmon import analyze as an
from perfmon import db, episodes, notes, regression
from perfmon.regression import game_key

RECENT_S = 14 * 86400          # a crash or a version change older than this is no longer news
MIN_PLAY_S = 120               # shorter finished sessions are a start and quit, not play: their FPS is the menu's
SESSIONS = 6                   # sessions listed on a game's page
TARGET_MAX = 1000
_assessed = {}                 # (finished session id, end ts) -> regression verdict (the baseline is earlier sessions)


def title(exe, ctx):
    """Steam's name of the game when the session context has one, else the exe name."""
    name = ((ctx or {}).get("steam") or {}).get("name") or ""
    return re.sub("[®™]", "", name).strip() or re.sub(r"(?i)\.exe$", "", exe or "")


def _fps(con, s, row):
    """How the game itself ran: gameplay only (menus and loading left out) and the game's own frames (frame generation
    left out), the numbers the comparison with earlier sessions uses. Falls back to the whole session.
    row: the session's row with id, start_ts, end_ts, summary."""
    if not s["running"]:
        try:
            m = regression.metrics(con, row)
        except Exception:
            m = None
        if m and m["usable"] and m["fps_avg"] is not None:
            return {"fps": m["fps_avg"], "fps_1low": m["fps_1low"]}
    fg = s.get("frame_gen_pct") and s.get("app_fps_avg") is not None
    return {"fps": s["app_fps_avg"] if fg else s.get("fps_avg"),
            "fps_1low": (s.get("app_fps_1low") if fg else None) or s.get("fps_1low")}


def _assess(con, s):
    """regression.assess of a finished session, kept; a session it cannot judge never breaks the screen."""
    key = (s["id"], s["end_ts"])
    if key not in _assessed:
        if len(_assessed) > 500:
            _assessed.clear()
        try:
            _assessed[key] = regression.assess(con, s["id"])
        except Exception:
            _assessed[key] = {"verdict": "insufficient"}
    return _assessed[key]


def _vs_usual(a):
    """FPS above (+) or below (-) the median of the earlier sessions, None without a baseline."""
    m = next((m for m in a.get("metrics") or [] if m["key"] == "fps_avg"), None)
    return round(m["value"] - m["baseline"]) if m else None


def _build(ctx):
    return (ctx.get("steam") or {}).get("buildid") or ctx.get("game_version")


def _cause(con, a, last):
    """What is new since the last good session, as far as a tile can say it: driver | game | None."""
    good = con.execute("SELECT context FROM sessions WHERE id=?", (a["last_good"]["session"],)).fetchone()
    old, new = an._json(good[0]) or {}, last["context"] or {}
    for kind, get in (("driver", an._driver), ("game", _build)):
        if get(old) and get(new) and get(old) != get(new):
            return kind
    return None


def _status(con, newest, last, test, now):
    """One line under a game: an open test, a recent crash, slower than usual, as usual, or not enough to tell.
    newest: the game's latest session; last: the one on the tile (the latest with frames)."""
    if test and game_key(test["exe"]) == game_key(last["exe"]):
        return {"kind": "test"}
    if newest["running"]:
        return {"kind": "live"}
    if newest["end_reason"] == "crash" and now - newest["start_ts"] < RECENT_S:   # also a crash before the first frame
        return {"kind": "crash", "ts": newest["end_ts"] or newest["start_ts"]}
    a = _assess(con, last)
    if a["verdict"] == "insufficient":
        return {"kind": "unknown"}
    if a["verdict"] == "regression":
        return {"kind": "slower", "cause": _cause(con, a, last)}
    return {"kind": "ok"}


def _played(con):
    """Game sessions, newest first, flattened (`row`: the raw row). Left out: launchers (no frames, clean exit) and
    a start and quit under MIN_PLAY_S; a crash or a cut-off session stays however short."""
    rows = an._rows(con, f"SELECT * FROM sessions WHERE {an.GAMES_ONLY} ORDER BY start_ts DESC, id DESC")
    ends = an._eff_ends(con, rows)
    out = []
    for r in rows:
        s = an._flatten_session(r, None, ends)
        clean = s["end_reason"] == "exit"
        if s["running"] or not clean or (s["frames"] and s["duration_s"] >= MIN_PLAY_S):
            s["row"] = r
            out.append(s)
    return out


def _stutters(con, sessions):
    ids = [s["id"] for s in sessions]
    n = dict(con.execute(f"SELECT session, COUNT(*) FROM stutters WHERE session IN ({','.join('?' * len(ids))}) "
                         "GROUP BY session", ids).fetchall()) if ids else {}
    for s in sessions:
        s["stutters"] = n.get(s["id"], 0)


def _brief(con, s):
    return {"id": s["id"], "start_ts": s["start_ts"], "duration_s": s["duration_s"], "running": s["running"],
            "end_reason": s["end_reason"], "frames": bool(s["frames"]), "stutters": s.get("stutters", 0),
            **_fps(con, s, s["row"])}


def _card(con, sessions, test, now):
    """sessions: one game's, newest first. The tile shows the newest one that has frames."""
    last = next((s for s in sessions if s["frames"] and not s["running"]), sessions[0])
    ctx = next((s["context"] for s in sessions if (s["context"] or {}).get("steam")), None) or last["context"]
    return {"key": game_key(last["exe"]), "exe": last["exe"], "title": title(last["exe"], ctx),
            "appid": ((ctx or {}).get("steam") or {}).get("appid"), "sessions": len(sessions),
            "running": sessions[0]["running"], "last": _brief(con, last),
            "status": _status(con, sessions[0], last, test, now)}


def _by_game(sessions):
    games = {}
    for s in sessions:
        games.setdefault(game_key(s["exe"]), []).append(s)
    return games


def games(con):
    """GET /api/games: one tile per game, last played first."""
    now, test = time.time(), notes.open_test(con)
    return [_card(con, ss, test, now) for ss in _by_game(_played(con)).values() if any(s["frames"] for s in ss)]


def _versions(sessions, now):
    """Game build, GPU driver and Windows build of the newest session. `since_ts`: when this game first ran on the
    current value, only when it replaced another one within RECENT_S (so the tile can say "new")."""
    get = {"game": lambda c: (f"build {c['steam']['buildid']}" if (c.get("steam") or {}).get("buildid")
                              else _build(c)),
           "driver": an._driver, "os": lambda c: c.get("os_build")}
    out = []
    for key, fn in get.items():
        vals = [(s["start_ts"], fn(s["context"] or {})) for s in sessions]   # newest first
        cur = next((v for _, v in vals if v), None)
        if not cur:
            continue
        since = changed = None
        for ts, v in vals:
            if v and v != cur:
                changed = True
                break
            if v:
                since = ts
        out.append({"key": key, "value": cur, "since_ts": since if changed and now - since < RECENT_S else None})
    return out


def _target_key(exe):
    return f"target_fps:{game_key(exe)}"


def game(con, exe):
    """GET /api/game?exe=: the tile's data plus target FPS, settings, versions, recent sessions and the agent card."""
    now = time.time()
    ss = _by_game(_played(con)).get(game_key(exe))
    if not ss:
        return {"error": f"Brak sesji gry {exe}"}
    recent = ss[:SESSIONS]
    card = _card(con, ss, notes.open_test(con), now)
    last = next(s for s in ss if s["id"] == card["last"]["id"])
    _stutters(con, recent + [last])   # the tile's session may be older than the listed ones
    a = _assess(con, last) if not last["running"] else {}
    target = db.get_meta(con, _target_key(exe))
    gs = next((g for g in (((s["context"] or {}).get("game_settings") or {}).get("normalized") for s in ss) if g), None)
    return {**card, "last": _brief(con, last), "vs_usual": _vs_usual(a),
            "baseline_sessions": len((a.get("baseline") or {}).get("sessions") or []),
            "target_fps": int(target) if target else None, "settings": gs, "versions": _versions(ss, now),
            "recent": [_brief(con, s) for s in recent], "agent": notes.card(con, last["exe"])}


def set_target(con, body):
    """POST /api/game_target {"exe", "fps"}: the FPS the user wants in this game; null or 0 clears it."""
    exe, fps = body.get("exe"), body.get("fps")
    if not exe:
        raise ValueError("brak gry")
    try:
        fps = int(fps or 0)
    except (TypeError, ValueError):
        raise ValueError("cel FPS musi być liczbą") from None
    if not fps:
        con.execute("DELETE FROM meta WHERE k=?", (_target_key(exe),))
        fps = None
    elif not 1 <= fps <= TARGET_MAX:
        raise ValueError(f"cel FPS: 1 do {TARGET_MAX}")
    else:
        db.set_meta(con, _target_key(exe), fps)
    con.commit()
    return {"target_fps": fps}


def home(con):
    """GET /api/overview: analyze.overview plus, per finished session, its gameplay FPS (`play`) and how that compares
    with the usual, and the agent card."""
    o = an.overview(con)
    ss = ([o["current"]] if o["current"] else []) + o["recent"]
    rows = {r["id"]: r for r in an._rows(con, f"SELECT id, start_ts, end_ts, summary FROM sessions WHERE id IN "
                                              f"({','.join('?' * len(ss))})", [s["id"] for s in ss])} if ss else {}
    for s in ss:
        s["play"] = None if s["running"] else _fps(con, s, rows[s["id"]])
        s["vs_usual"] = None if s["running"] else _vs_usual(_assess(con, s))
    o["agent"] = notes.card(con)
    return o


# --------------------------------------------------------------------------
# problems
# --------------------------------------------------------------------------

# kind -> (group, severity, gap in seconds under which two rows are one occurrence, how far back the last sample
# may be to tell what was running). A hard reset is logged at the next boot, so it looks back for hours.
_FREEZE = ("freeze", "warning", 30, 15)
_DISK = ("disk", "serious", 300, 15)
PROBLEMS = {
    "unclean_shutdown": ("reset", "critical", 600, 6 * 3600), "bsod": ("bsod", "critical", 600, 6 * 3600),
    "gpu_driver": ("gpu_driver", "critical", 60, 15), "whea_fatal": ("whea", "critical", 60, 15),
    "whea": ("whea", "serious", 60, 15), "app_crash": ("crash", "serious", 30, 15), "app_hang": ("hang", "warning", 30, 15),
    "ui_freeze": _FREEZE, "dwm_stall": _FREEZE, "app_not_responding": _FREEZE,
    "nvme_reset": _DISK, "disk_retry": _DISK, "disk_removed": _DISK, "disk_health": _DISK, "disk": _DISK,
    "low_memory": ("memory", "serious", 300, 15), "commit_full": ("memory", "serious", 300, 15),
    "game_mem_leak": ("leak", "warning", 3600, 15), "malware": ("malware", "serious", 300, 15),
    "net_down": ("net", "warning", 300, 15),
}
PROBLEM_ROWS = 30


def _game_at(con, ts, back, cache):
    """((exe, title) of the game session the last sample before `ts` belonged to, None outside a game; that sample's
    ts, None when there is none within `back` seconds)."""
    r = con.execute("SELECT game_session, ts FROM samples WHERE ts<=? AND ts>? ORDER BY ts DESC LIMIT 1",
                    (ts, ts - back)).fetchone()
    if not r:
        return None, None
    sid = r[0]
    if sid is not None and sid not in cache:
        s = con.execute(f"SELECT exe, context FROM sessions WHERE id=? AND {an.GAMES_ONLY}", (sid,)).fetchone()
        cache[sid] = (s["exe"], title(s["exe"], an._json(s["context"]))) if s else None
    return cache.get(sid), r[1]


def problems(con, days=14):
    """GET /api/problems: what went wrong lately in plain words, newest first, repeats folded into one row.

    Row: {group, severity, ts (of the latest one), count, game (title, None outside a game), app (the crashed program when it
    is not a game; such a row has no game), duration_s (longest freeze)}. The window words the row from `group`."""
    t0 = time.time() - float(days) * 86400
    rows = an.incidents_between(con, t0, time.time() + 1, list(PROBLEMS), limit=100000, group=True)
    known = {}   # game key -> (exe, title), the title from the newest session that knows Steam's name
    for r in con.execute(f"SELECT exe, context FROM sessions WHERE {an.GAMES_ONLY} ORDER BY id"):
        ctx = an._json(r["context"]) or {}
        if game_key(r["exe"]) not in known or ctx.get("steam"):
            known[game_key(r["exe"])] = (r["exe"], title(r["exe"], ctx))
    # 1. occurrences: rows of one group (and program) closer than the group's gap are one event, whatever each row
    #    says about the game: of a reset's two log entries only the first is stamped before the post-boot samples
    cache, events, open_ = {}, [], {}
    for r in rows:   # oldest first
        group, sev, gap, back = PROBLEMS[r["kind"]]
        at, seen = _game_at(con, r["ts"], back, cache)
        app = None
        if group in ("crash", "hang"):
            app = r["title"].split(":", 1)[-1].strip()
            if game_key(app) in known:   # the game itself went down, also when its session had already closed
                at, app = at or known[game_key(app)], None
            else:
                at = None   # another program: the row names it, whatever game was running
        e = open_.get((group, app))
        if e is None or r["ts"] - e["ts"] > gap:
            # a reset is logged at the next boot: it happened when the samples stopped
            e = open_[(group, app)] = {"group": group, "severity": sev, "at": at, "app": app, "duration_s": None,
                                       "when": seen if group in ("reset", "bsod") and seen else r["ts"]}
            events.append(e)
        e["ts"], e["at"] = r["ts"], e["at"] or at
        if episodes._SEV[sev] > episodes._SEV[e["severity"]]:
            e["severity"] = sev
        if group == "freeze" and r["end_ts"]:
            e["duration_s"] = max(e["duration_s"] or 0, round(r["end_ts"] - r["ts"], 1))
    # 2. rows: the same thing happening again to the same game or program is one row with a count
    out = {}
    for e in events:
        at, when = e.pop("at"), e.pop("when")
        row = out.setdefault((e["group"], game_key(at[0]) if at else None, e["app"]),
                             {**e, "count": 0, "game": at[1] if at else None})
        row["count"] += 1
        row["ts"] = when
        if episodes._SEV[e["severity"]] > episodes._SEV[row["severity"]]:
            row["severity"] = e["severity"]
        if e["duration_s"]:
            row["duration_s"] = max(row["duration_s"] or 0, e["duration_s"])
    items = sorted(out.values(), key=lambda x: -x["ts"])[:PROBLEM_ROWS]
    return {"days": days, "items": items, "agent": notes.card(con, general=True)}
