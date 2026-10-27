"""How a game presents its frames (PresentMon PresentMode / runtime / sync / latency): session section,
per-session label for lists and comparisons, and the 'presenting through DWM' finding."""
from . import analyze as an

LABELS = {"iflip": "IFlip", "fse": "FSE", "composed": "Composed"}
LABEL_MIN_PCT = 90       # share of frames in one class for a clean label, otherwise "mixed"
COMPOSED_WARN_PCT = 20   # composed share that earns a finding


def classify(mode):
    """PresentMode -> 'iflip' (DWM bypassed), 'fse' (legacy exclusive fullscreen), 'composed' (through DWM)."""
    m = (mode or "").strip()
    if "Independent Flip" in m:      # "Hardware: Independent Flip", "Hardware Composed: Independent Flip" (MPO)
        return "iflip"
    if m.startswith("Hardware"):     # "Hardware: Legacy Flip", "Hardware: Legacy Copy to front buffer"
        return "fse"
    if m.startswith("Composed"):     # "Composed: Flip", "Composed: Copy with GPU GDI", "Composed: Copy with CPU GDI"
        return "composed"
    return None                      # Unknown / empty


def class_shares(modes):
    """{PresentMode: pct or frames} -> {class: pct} over the classified frames."""
    acc = {}
    for m, n in (modes or {}).items():
        c = classify(m)
        if c and n:
            acc[c] = acc.get(c, 0) + n
    tot = sum(acc.values())
    return {c: round(100 * n / tot, 1) for c, n in acc.items()} if tot else {}


def label(shares):
    if not shares:
        return None
    c, pct = max(shares.items(), key=lambda kv: kv[1])
    return LABELS[c] if pct >= LABEL_MIN_PCT else "mixed"


def labels(con, ids):
    """{session id: IFlip|FSE|Composed|mixed} from the per-second frames (frame-weighted)."""
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    modes = {}
    for sid, mode, n in con.execute(f"SELECT session, present_mode, SUM(frames) FROM frames WHERE session IN ({q}) "
                                    f"GROUP BY session, present_mode", list(ids)):
        modes.setdefault(sid, {})[mode] = n or 0
    return {sid: label(class_shares(m)) for sid, m in modes.items()}


def _top(con, col, sid):
    r = con.execute(f"SELECT {col}, SUM(frames) AS n FROM frames WHERE session=? AND {col} IS NOT NULL "
                    f"GROUP BY {col} ORDER BY n DESC LIMIT 1", (sid,)).fetchone()
    return r[0] if r else None


def _from_frames(con, sid):
    modes = {m: n for m, n in con.execute("SELECT present_mode, SUM(frames) FROM frames WHERE session=? "
                                          "AND present_mode IS NOT NULL AND present_mode<>'' GROUP BY present_mode", (sid,))}
    if not modes:
        return None
    tot = sum(modes.values()) or 1

    def wavg(col, scale=1.0):
        r = con.execute(f"SELECT SUM({col}*frames), SUM(frames) FROM frames WHERE session=? AND {col} IS NOT NULL",
                        (sid,)).fetchone()
        return round(scale * r[0] / r[1], 1) if r and r[1] else None

    dr = con.execute("SELECT SUM(dropped), SUM(frames) FROM frames WHERE session=? AND dropped IS NOT NULL",
                     (sid,)).fetchone()
    return {
        "runtime": _top(con, "runtime", sid),
        "modes": {m: round(100 * n / tot, 1) for m, n in sorted(modes.items(), key=lambda kv: -kv[1])},
        "sync_interval": _top(con, "sync_interval", sid),
        "tearing_pct": wavg("tearing", 100.0),
        "dropped_pct": round(100 * dr[0] / dr[1], 2) if dr and dr[1] else None,
        "click_lat_avg": wavg("click_lat"),
        "input_lat_avg": wavg("input_lat"),
        "frame_gen_pct": wavg("frame_gen_pct"),
    }


def session_present(con, row, summ):
    """summary.present (whole capture) or, for older/running sessions, the same computed from frames."""
    p = dict((summ or {}).get("present") or {}) or _from_frames(con, row["id"])
    if not p:
        return None
    shares = class_shares(p.get("modes"))
    p["composed_pct"] = shares.get("composed", 0.0) if shares else None
    p["direct_pct"] = round(shares.get("iflip", 0.0) + shares.get("fse", 0.0), 1) if shares else None
    p["label"] = label(shares)
    return p


def describe(p):
    """One line for `pm session`."""
    if not p:
        return None
    parts = [p.get("runtime")]
    parts.append(" / ".join(f"{m} {v}%" for m, v in list((p.get("modes") or {}).items())[:3]))
    if p.get("sync_interval") is not None:
        parts.append(f"vsync {'wył.' if p['sync_interval'] == 0 else p['sync_interval']}")
    if p.get("tearing_pct") is not None:
        parts.append(f"tearing {p['tearing_pct']}%")
    if p.get("dropped_pct"):
        parts.append(f"niewyświetlone {p['dropped_pct']}%")
    if p.get("click_lat_avg") is not None:
        parts.append(f"klik->obraz {p['click_lat_avg']} ms")
    if p.get("input_lat_avg") is not None:
        parts.append(f"wejście->obraz {p['input_lat_avg']} ms")
    if p.get("frame_gen_pct"):
        parts.append(f"generowane klatki {p['frame_gen_pct']}%")
    return f"{p.get('label') or '?'}: " + ", ".join(x for x in parts if x)


def findings(rep):
    p = rep.get("present") or {}
    pct = p.get("composed_pct")
    if not pct or pct < COMPOSED_WARN_PCT:
        return []
    return [{"severity": "warning" if pct >= 50 else "info",
             "title": f"Gra prezentuje przez DWM (Composed) przez {an._r(pct)}% klatek",
             "detail": "Klatki idą przez kompozytor pulpitu zamiast trybu Independent Flip: większe opóźnienie i "
                       "stuttery przy każdym zacięciu DWM. Wyłącz overlaye (Discord, Steam, Adrenalin, Xbox Game Bar), "
                       "ustaw pełny ekran zamiast okna bez ramki, nie zasłaniaj gry innymi oknami; MPO (Multiplane "
                       "Overlay) w sterowniku musi być włączone. Composed Flip przy dwóch monitorach o różnym "
                       "odświeżaniu (144 Hz + 59 Hz) to układ podejrzany o zawieszenia DWM."}]
