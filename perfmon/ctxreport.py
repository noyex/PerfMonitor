"""What changed between sessions: game settings, system snapshot and session context (driver, OS, game build),
for the session report (rep["config"]) and `pm compare` (settings / changes per group). Read-only."""
import json

from . import confdiff

# sessions.context keys from gamectx (driver, OS, game build) worth naming when they changed between sessions
CTX_LABELS = {"gpu_driver": "sterownik GPU", "adrenalin": "Adrenalin", "os_build": "Windows (build)", "bios": "BIOS",
              "game_version": "wersja gry", "steam.buildid": "build Steam"}


def _json(s):
    try:
        v = json.loads(s) if s else None
    except (TypeError, ValueError):
        return None
    return v


def _ctx(row):
    c = _json(row["context"]) if row is not None else None
    return c if isinstance(c, dict) else {}


def snapshot_at(con, ctx, start_ts, cache=None):
    """system_info dict in effect for a session: the one its context points at, else the latest before its start."""
    ts = ctx.get("system_snapshot_ts")
    key = ts if ts is not None else ("<=", start_ts)
    if cache is not None and key in cache:
        return cache[key]
    if ts is not None:
        r = con.execute("SELECT info FROM system_info WHERE ts=?", (ts,)).fetchone()
    else:
        r = con.execute("SELECT info FROM system_info WHERE ts<=? ORDER BY ts DESC LIMIT 1", (start_ts,)).fetchone()
    info = _json(r[0]) if r else None
    if cache is not None:
        cache[key] = info
    return info


def _ctx_changes(old, new):
    out = []
    for k, lbl in CTX_LABELS.items():
        a = (old.get("steam") or {}).get("buildid") if k == "steam.buildid" else old.get(k)
        b = (new.get("steam") or {}).get("buildid") if k == "steam.buildid" else new.get(k)
        if a and b and a != b:
            out.append(f"{lbl}: {a} → {b}")
    return out


def _game_changes(old, new):
    ga, gb = old.get("game_settings") or {}, new.get("game_settings") or {}
    if not ga.get("normalized") or not gb.get("normalized") or ga.get("hash") == gb.get("hash"):
        return [], 0
    texts = [c["text"] for c in confdiff.diff(ga["normalized"], gb["normalized"], volatile=set())]
    raw = len(gb.get("changed") or {}) if gb.get("prev_hash") == ga.get("hash") else None
    return texts, raw


def changes_between(con, old_row, new_row, cache=None):
    """{"game": [...], "game_raw_changed": n, "system": [...], "context": [...]} between two sessions rows."""
    a, b = _ctx(old_row), _ctx(new_row)
    game, raw = _game_changes(a, b)
    sa = snapshot_at(con, a, old_row["start_ts"], cache)
    sb = snapshot_at(con, b, new_row["start_ts"], cache)
    system = [c["text"] for c in confdiff.diff(sa, sb)] if sa and sb else []
    return {"game": game, "game_raw_changed": raw, "system": system, "context": _ctx_changes(a, b)}


def extend(con, rep, row):
    """rep["config"]: settings in effect + changes vs the previous session of the same game; adds a finding."""
    ctx = _ctx(row)
    summ = rep.get("summary") or {}
    prev = con.execute("SELECT * FROM sessions WHERE lower(exe)=lower(?) AND start_ts<? AND COALESCE(kind,'game')='game' "
                       "ORDER BY start_ts DESC LIMIT 1", (row["exe"], row["start_ts"])).fetchone()
    gs = ctx.get("game_settings") or {}
    snap = snapshot_at(con, ctx, row["start_ts"]) or {}
    cfg = {"game_settings": gs.get("normalized"), "game_settings_error": gs.get("error"),
           "game_settings_files": [f.get("path") for f in gs.get("files") or []] or None,
           "system_snapshot_ts": ctx.get("system_snapshot_ts"),
           "amd": snap.get("amd"), "power": snap.get("power"), "monitor_state": snap.get("monitor_state"),
           "mid_session": summ.get("settings_changes") or [], "modules": summ.get("modules"),
           "modules_changes": summ.get("modules_changes") or [],
           "previous_session": prev["id"] if prev else None, "changes": None}
    if prev is not None:
        cfg["changes"] = changes_between(con, prev, row)
    rep["config"] = cfg
    ch = cfg["changes"] or {}
    texts = (ch.get("context") or []) + (ch.get("game") or []) + (ch.get("system") or [])
    if texts:
        more = f" (+{len(texts) - 6})" if len(texts) > 6 else ""
        rep.setdefault("findings", []).append({
            "severity": "info", "title": f"Zmiany od poprzedniej sesji #{prev['id']}",
            "detail": "; ".join(texts[:6]) + more + ". Różnica w FPS/stutterach może wynikać z tych zmian."})
    for e in cfg["mid_session"]:
        if e.get("changes"):
            rep.setdefault("findings", []).append({"severity": "info", "title": "Zmiana ustawień gry w trakcie sesji",
                                                   "detail": "; ".join(e["changes"])})
    return cfg


def annotate_compare(con, rows, ctx_by_id):
    """pm compare rows (sorted per exe by time): settings of each group's latest session and what changed vs the
    previous group of the same game (game settings, system snapshot, driver / OS / game build)."""
    cache, prev = {}, {}
    for r in rows:
        ids = [i for i in r.get("session_ids") or [] if i in ctx_by_id]
        last = max(ids, key=lambda i: ctx_by_id[i].get("_start") or 0) if ids else None
        ctx = ctx_by_id.get(last) or {}
        r["settings"] = (ctx.get("game_settings") or {}).get("normalized")
        hashes = {(ctx_by_id[i].get("game_settings") or {}).get("hash") for i in ids} - {None}
        r["settings_mixed"] = len(hashes) > 1   # the group itself mixes several game settings
        p = prev.get(r["exe"])
        r["changes"] = None
        if p is not None and last is not None:
            old = {"context": json.dumps(p[1]), "start_ts": p[1].get("_start")}
            new = {"context": json.dumps(ctx), "start_ts": ctx.get("_start")}
            ch = changes_between(con, old, new, cache)
            r["changes"] = ch["context"] + ch["game"] + ch["system"]
        if last is not None:
            prev[r["exe"]] = (last, ctx)
    return rows
