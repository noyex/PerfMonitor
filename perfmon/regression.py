"""Does a session play worse (or better) than the earlier sessions of the same game, and what changed in between?

Baseline: up to BASELINE_N earlier usable sessions of the same game with at least MIN_MINUTES of gameplay (menus and
loading excluded, perfmon/gameplay.py). Robust statistics: the baseline median, and its session-to-session spread as
1.4826 x MAD. A metric moved only when the change beats the minimum relative change, an absolute floor and NOISE_K
times that spread; anything smaller is reported as within noise, never as a regression. The context diff is generic
(every key of sessions.context, nested ones flattened), so settings other code adds to the context show up without
changes here, plus config_change / gpu_settings_change incidents and the UV/OC profile between the last good session
and this one."""
import json
import re
import time
from datetime import datetime

from . import gameplay

BASELINE_N = 8
MIN_BASELINE = 3          # fewer comparable sessions: no verdict
MIN_MINUTES = 10          # gameplay minutes of the session and of each baseline session
NOISE_K = 2.0
# key, label, direction (+1 higher is better), minimum relative change, absolute floor
METRICS = (("fps_avg", "średni FPS", 1, 0.05, 1.0), ("fps_1low", "1% low", 1, 0.07, 1.0),
           ("stutters_per_min", "stuttery/min", -1, 0.30, 0.3), ("click_lat_avg", "opóźnienie klik→obraz", -1, 0.10, 2.0))
SKIP_CTX = re.compile(r"(_mtime|last_updated|_date)$")   # volatile or duplicate of a version key
CHANGE_KINDS = ("config_change", "gpu_settings_change")
GAME_ALIAS = re.compile(r"^(sp|mp|zm)\d*-", re.I)    # sp26-cod.exe and cod.exe are one game
NO_PROBLEMS = "Brak wykrytych problemów"            # analyze._session_findings' all-clear finding
SEVERITY_ORDER = {"critical": 0, "serious": 1, "warning": 2, "info": 3}
_STATS = {}               # (session id, end ts) -> gameplay stats computed for sessions stored without them


def game_key(exe):
    return GAME_ALIAS.sub("", (exe or "").lower())


def _json(s):
    try:
        v = json.loads(s) if s else None
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def metrics(con, r):
    """{fps_avg, fps_1low, stutters_per_min, click_lat_avg, minutes} of a finished session row, or None when it has no
    gameplay numbers. Sessions stored before summary.gameplay existed are judged on the fly (memoized)."""
    summ = _json(r["summary"])
    gp = summ.get("gameplay")
    if gp is None and r["end_ts"]:
        key = (r["id"], r["end_ts"])
        if key not in _STATS:
            _STATS[key] = gameplay.stats(con, r["id"], r["start_ts"], r["end_ts"])
        gp = _STATS[key]
    if not gp or not gp.get("seconds"):
        return None
    return {"fps_avg": gp.get("fps_avg"), "fps_1low": gp.get("fps_1low"), "stutters_per_min": gp.get("stutters_per_min"),
            "click_lat_avg": (summ.get("present") or {}).get("click_lat_avg"), "minutes": gp["seconds"] / 60,
            "usable": gp.get("usable", True)}


def _median(vs):
    vs = sorted(vs)
    n = len(vs)
    return (vs[n // 2] + vs[(n - 1) // 2]) / 2


def _flat(d, prefix=""):
    """{"steam": {"buildid": 1}, "gpus": [{"name": "X", "driver": 2}]} -> {"steam.buildid": 1, "gpus[X].driver": 2}."""
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        elif isinstance(v, list) and v and all(isinstance(x, dict) and x.get("name") for x in v):
            for x in v:
                out.update(_flat({kk: vv for kk, vv in x.items() if kk != "name"}, f"{prefix}{k}[{x['name']}]."))
        elif not SKIP_CTX.search(k):
            out[prefix + k] = v
    return out


def _short(v):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= 40 else s[:39] + "…"


def _judge(cur, base, min_n=MIN_BASELINE):
    """Per-metric verdicts of `cur` against the baseline metric dicts."""
    out = []
    for key, label, sign, rel, floor in METRICS:
        vals = [b[key] for b in base if b.get(key) is not None]
        v = cur.get(key)
        if v is None or len(vals) < min_n:
            continue
        med = _median(vals)
        spread = 1.4826 * _median([abs(x - med) for x in vals])
        thr = max(rel * abs(med), floor, NOISE_K * spread)
        d = v - med
        verdict = "noise" if abs(d) <= thr else ("improvement" if d * sign > 0 else "regression")
        out.append({"key": key, "label": label, "value": round(v, 1), "baseline": round(med, 1),
                    "change_pct": round(100 * d / med, 1) if med else None,
                    "noise_pct": round(100 * thr / abs(med), 1) if med else None, "verdict": verdict})
    return out


def _rows(con, exe=None):
    sql = ("SELECT id, exe, start_ts, end_ts, summary, context, profile_id FROM sessions "
           "WHERE end_ts IS NOT NULL AND end_ts > start_ts AND COALESCE(kind, 'game')='game'")
    return [r for r in con.execute(sql + " ORDER BY start_ts") if exe is None or game_key(r["exe"]) == game_key(exe)]


def _comparable(con, rows):
    out = []
    for r in rows:
        m = metrics(con, r)
        if m and m["usable"] and m["minutes"] >= MIN_MINUTES:
            out.append((r, m))
    return out


def assess(con, session_id, rows=None):
    """Verdict for one finished session against its baseline; see the module docstring."""
    r = con.execute("SELECT id, exe, start_ts, end_ts, summary, context, profile_id FROM sessions WHERE id=?",
                    (int(session_id),)).fetchone()
    if not r or r["end_ts"] is None:
        return {"session": session_id, "verdict": "insufficient", "reason": "sesja trwa"}
    cur = metrics(con, r)
    if not cur or not cur["usable"] or cur["minutes"] < MIN_MINUTES:
        return {"session": r["id"], "exe": r["exe"], "verdict": "insufficient",
                "reason": f"za mało rozgrywki (potrzeba {MIN_MINUTES} min)"}
    rows = rows if rows is not None else _rows(con, r["exe"])
    earlier = [x for x in _comparable(con, [x for x in rows if x["start_ts"] < r["start_ts"]
                                            and game_key(x["exe"]) == game_key(r["exe"])])][-BASELINE_N:]
    if len(earlier) < MIN_BASELINE:
        return {"session": r["id"], "exe": r["exe"], "verdict": "insufficient",
                "reason": f"za mało porównywalnych wcześniejszych sesji ({len(earlier)} z {MIN_BASELINE})"}
    base = [m for _, m in earlier]
    judged = _judge(cur, base)
    worse = [j for j in judged if j["verdict"] == "regression"]
    better = [j for j in judged if j["verdict"] == "improvement"]
    verdict = "mixed" if worse and better else "regression" if worse else "improvement" if better else "noise"
    # last good: the newest baseline session that is not itself a regression against the others
    good = None
    for x, m in reversed(earlier):
        others = [o for y, o in earlier if y["id"] != x["id"]]
        if not any(j["verdict"] == "regression" for j in _judge(m, others, MIN_BASELINE - 1)
                   if j["key"] in ("fps_avg", "fps_1low")):
            good = x
            break
    good = good or earlier[-1][0]
    out = {"session": r["id"], "exe": r["exe"], "verdict": verdict, "metrics": judged,
           "minutes": round(cur["minutes"], 1),
           "baseline": {"sessions": [x["id"] for x, _ in earlier], "minutes": round(sum(m["minutes"] for m in base))},
           "last_good": {"session": good["id"], "time": _fmt(good["start_ts"])}}
    out["changes"] = context_changes(con, good, r)
    out["message"] = message(out)
    return out


def context_changes(con, before, after):
    """[{key, before, after}] of the session context, the UV/OC profile, and config incidents in between."""
    a, b = _flat(_json(before["context"])), _flat(_json(after["context"]))
    out = [{"key": k, "before": a.get(k), "after": b.get(k)} for k in sorted(set(a) | set(b))
           if a.get(k) != b.get(k) and a.get(k) is not None and b.get(k) is not None]
    if before["profile_id"] != after["profile_id"]:
        out.append({"key": "oc_profile", "before": before["profile_id"], "after": after["profile_id"]})
    for ts, kind, title, det in con.execute(
            f"SELECT ts, kind, title, details FROM incidents WHERE kind IN ({','.join('?' * len(CHANGE_KINDS))}) "
            "AND ts > ? AND ts < ? ORDER BY ts", (*CHANGE_KINDS, before["end_ts"], after["start_ts"])):
        d = _json(det)
        if kind == "config_change" and d:
            have = {c["key"] for c in out}
            out += [{"key": k, "before": v.get("before"), "after": v.get("after"), "time": _fmt(ts)}
                    for k, v in d.items() if isinstance(v, dict) and k not in have]
        else:
            out.append({"key": kind, "before": None, "after": title, "time": _fmt(ts)})
    return out


def _metric_text(j):
    return f"{j['label']} {j['baseline']:g} → {j['value']:g} ({j['change_pct']:+.0f}%)" if j["change_pct"] is not None \
        else f"{j['label']} {j['baseline']:g} → {j['value']:g}"


def message(a):
    v = a["verdict"]
    n = len(a["baseline"]["sessions"])
    moved = [j for j in a["metrics"] if j["verdict"] != "noise"]
    if v == "noise":
        return (f"Wyniki w granicach normalnej zmienności między sesjami (porównanie z {n} wcześniejszymi sesjami, "
                f"{a['baseline']['minutes']} min rozgrywki).")
    head = {"regression": "Gorzej niż zwykle", "improvement": "Lepiej niż zwykle", "mixed": "Część metryk gorzej, część lepiej"}[v]
    txt = f"{head} względem {n} wcześniejszych sesji: " + "; ".join(_metric_text(j) for j in moved) + "."
    if a["changes"]:
        txt += (f" Od ostatniej dobrej sesji ({a['last_good']['time']}) zmieniło się: "
                + ", ".join(f"{c['key']} {_short(c['before'])} → {_short(c['after'])}" if c["before"] is not None
                            else _short(c["after"]) for c in a["changes"][:6])
                + (f" i {len(a['changes']) - 6} więcej" if len(a["changes"]) > 6 else "") + ".")
    else:
        txt += (" Zapisany kontekst sesji (sterownik, wersja gry, system, BIOS, ustawienia) się nie zmienił, więc przyczyna "
                "leży poza nim (np. ustawienia w grze, mapa, tryb).")
    return txt


def finding(a):
    """session_report finding, or None (no verdict, or nothing moved at all)."""
    v = a.get("verdict")
    if v in ("regression", "mixed"):
        worst = min((j for j in a["metrics"] if j["verdict"] == "regression"), key=lambda j: -abs(j["change_pct"] or 0))
        return {"severity": "warning", "kind": "perf_regression",
                "title": f"Spadek wydajności: {_metric_text(worst)}", "detail": a["message"]}
    if v == "improvement":
        best = max((j for j in a["metrics"] if j["verdict"] == "improvement"), key=lambda j: abs(j["change_pct"] or 0))
        return {"severity": "info", "kind": "perf_improvement",
                "title": f"Poprawa wydajności: {_metric_text(best)}", "detail": a["message"]}
    if v == "noise" and any(abs(j["change_pct"] or 0) >= 100 * dict((m[0], m[3]) for m in METRICS)[j["key"]]
                            for j in a["metrics"]):
        return {"severity": "info", "kind": "perf_noise", "title": "Zmiana wydajności w granicach szumu",
                "detail": a["message"]}
    return None


def attach(con, rep, row):
    """Add `regression` and its finding to a session_report (finished game sessions only)."""
    if row["end_ts"] is None or (row.get("kind") or "game") != "game":
        return
    try:
        a = assess(con, row["id"])
    except Exception as e:   # never break the report over the comparison
        a = {"session": row["id"], "verdict": "insufficient", "reason": f"{type(e).__name__}: {e}"}
    rep["regression"] = a
    f = finding(a)
    if f:
        fs = rep.setdefault("findings", [])
        if f["severity"] != "info":   # the session is no longer "healthy"
            fs[:] = [x for x in fs if x["title"] != NO_PROBLEMS]
        fs.append(f)
        fs.sort(key=lambda x: SEVERITY_ORDER.get(x["severity"], 9))


def overview(con, exe=None, limit=20):
    """Latest verdicts per session (newest first), for `pm regress` and /api/regressions."""
    rows = _rows(con, exe)
    t0 = time.time()
    out = []
    for r in reversed(rows):
        if len(out) >= limit or time.time() - t0 > 20:
            break
        a = assess(con, r["id"], rows)
        if a["verdict"] != "insufficient" or exe:
            a["time"] = _fmt(r["start_ts"])
            out.append(a)
    return out
