"""`pm test`: change ONE setting, keep playing, get a verdict whether it helped. NX Frame never changes anything.

Compares the last usable sessions of a game before the test started with the sessions played after; a change only counts
when it is bigger than the session-to-session noise."""
import json
import math
import time

from perfmon import analyze as an

MIN_MIN, MIN_SESSIONS, MAX_BEFORE, BEFORE_DAYS, REL_FLOOR = 30, 2, 8, 30, 0.03
METRICS = (("fps_avg", "FPS średnie", True), ("fps_1low", "FPS 1% low", True),
           ("stutters_per_min", "przycięcia/min", False))   # (key, label, higher is better)


def _open(con):
    r = con.execute("SELECT * FROM experiments WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def _get(con, eid):
    if eid is None:
        r = _open(con)
        if not r:
            raise ValueError("Brak trwającego testu.")
        return r
    r = con.execute("SELECT * FROM experiments WHERE id=?", (int(eid),)).fetchone()
    if not r:
        raise ValueError(f"Brak testu #{eid}.")
    return dict(r)


def default_exe(con, days=14):
    """The game with the most gameplay seconds in the last `days` days."""
    best = {}
    for r in con.execute(f"SELECT exe, summary FROM sessions WHERE start_ts>=? AND {an.GAMES_ONLY}",
                         (time.time() - days * 86400,)):
        sec = ((an._json(r["summary"]) or {}).get("gameplay") or {}).get("seconds")
        if sec:
            best[r["exe"]] = best.get(r["exe"], 0) + sec
    return max(best, key=best.get) if best else None


def start(con, name, exe=None, key=None, now=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("Podaj nazwę testu, np. 'Instant Replay wyłączony'.")
    o = _open(con)
    if o:
        raise ValueError(f"Trwa już test #{o['id']} ({o['name']}). Dwóch zmian naraz nie da się rozdzielić: "
                         f"zakończ go (pm test stop) albo anuluj (pm test cancel).")
    exe = exe or default_exe(con)
    if not exe:
        raise ValueError("Brak gier z danymi z ostatnich 14 dni: podaj grę (--exe).")
    cur = con.execute("INSERT INTO experiments(name, key, exe, start_ts, status) VALUES (?,?,?,?,'open')",
                      (name, key or None, exe, now or time.time()))
    con.commit()
    return cur.lastrowid


def _group(ss):
    g = {"sessions": len(ss), "minutes": round(sum(s["minutes"] or 0 for s in ss), 1)}
    for k, _, _ in METRICS:
        vs = [(s[k], s["minutes"] or 0) for s in ss if s.get(k) is not None]
        w = sum(m for _, m in vs)
        g[k] = sum(v * m for v, m in vs) / w if w > 0 else None
        g["_" + k] = [v for v, _ in vs]
    return g


def _sd(vs):
    if len(vs) < 2:
        return 0.0
    mu = sum(vs) / len(vs)
    return math.sqrt(sum((v - mu) ** 2 for v in vs) / (len(vs) - 1))


def _metric(key, label, up, b, a):
    mb, ma = b[key], a[key]
    if mb is None or ma is None:
        return None
    diff = ma - mb
    vb, va = b["_" + key], a["_" + key]
    noise = 2 * math.sqrt(_sd(vb) ** 2 / len(vb) + _sd(va) ** 2 / len(va))
    return {"key": key, "label": label, "before": round(mb, 2), "after": round(ma, 2), "change_pct":
            round(100 * diff / mb, 1) if mb else None, "noise": round(noise, 2),
            "significant": abs(diff) > max(REL_FLOOR * abs(mb), noise),
            "better": ((diff > 0) == up) if diff else None}


def evaluate(con, exp):
    exe, t0, t1 = exp["exe"], exp["start_ts"], exp.get("end_ts")
    ms = [s for s in an._session_metrics(con, exe) if s["usable"] and s["end_ts"]]   # running sessions are left out
    before = [s for s in ms if t0 - BEFORE_DAYS * 86400 <= s["end_ts"] <= t0][-MAX_BEFORE:]
    after = [s for s in ms if s["start_ts"] >= t0 and (t1 is None or s["end_ts"] <= t1)]
    gb, ga = _group(before), _group(after)
    missing = []
    for g, where in ((gb, "przed zmianą"), (ga, "po zmianie")):
        if g["minutes"] < MIN_MIN:
            missing.append(f"brakuje {math.ceil(MIN_MIN - g['minutes'])} min gry {where}")
        elif g["sessions"] < MIN_SESSIONS:
            missing.append(f"brakuje sesji {where} (min. {MIN_SESSIONS})")
    warnings = []
    for field, what in (("gpu_driver", "sterownik GPU"), ("game_version", "wersja gry")):
        vb, va = {s[field] for s in before if s[field]}, {s[field] for s in after if s[field]}
        if vb and va and vb != va:
            warnings.append(f"Zmienił się {what} między grupami: wyniku nie da się przypisać tej zmianie.")
    ov = con.execute("SELECT name FROM experiments WHERE id!=? AND status!='cancelled' AND start_ts<? AND "
                     "COALESCE(end_ts, ?)>?", (exp["id"], t1 or time.time(), time.time(), t0)).fetchall()
    if ov:
        warnings.append(f"Inny test nakładał się w czasie ({', '.join(r[0] for r in ov)}): zmian nie da się rozdzielić.")
    mets = [] if missing else [m for m in (_metric(k, l, u, gb, ga) for k, l, u in METRICS) if m]
    if missing:
        verdict = "za mało danych"
    else:
        good = any(m["significant"] and m["better"] for m in mets)
        bad = any(m["significant"] and m["better"] is False for m in mets)
        verdict = "mieszane" if good and bad else "pomogło" if good else "pogorszyło" if bad else "bez zmian"
    pub = lambda g: {k: v for k, v in g.items() if not k.startswith("_")}
    return {"id": exp["id"], "name": exp["name"], "key": exp["key"], "exe": exe, "status": exp["status"],
            "start_ts": t0, "end_ts": t1, "verdict": verdict, "missing": missing, "metrics": mets,
            "before": pub(gb), "after": pub(ga), "warnings": warnings}


def _close(con, exp, status):
    if exp["status"] != "open":
        raise ValueError(f"Test #{exp['id']} jest już zamknięty ({exp['status']}).")
    end = time.time()
    res = evaluate(con, dict(exp, end_ts=end, status="done")) if status == "done" else None
    con.execute("UPDATE experiments SET status=?, end_ts=?, result=? WHERE id=?",
                (status, end, json.dumps(res, ensure_ascii=False) if res else None, exp["id"]))
    con.commit()
    return res


def stop(con, eid=None):
    return _close(con, _get(con, eid), "done")


def cancel(con, eid=None):
    exp = _get(con, eid)
    _close(con, exp, "cancelled")
    return exp["id"]


def list_all(con):
    rows = []
    for r in con.execute("SELECT * FROM experiments ORDER BY id DESC"):
        res = an._json(r["result"]) if r["result"] else None
        rows.append({"id": r["id"], "name": r["name"], "key": r["key"], "exe": r["exe"], "status": r["status"],
                     "start_ts": r["start_ts"], "end_ts": r["end_ts"], "time": an.fmt_ts(r["start_ts"]),
                     "verdict": (res or {}).get("verdict")})
    return rows


def show(con, eid=None):
    r = _get(con, eid)
    if r["status"] == "open":
        return evaluate(con, r)
    return (an._json(r["result"]) if r["result"] else None) or {
        "id": r["id"], "name": r["name"], "status": r["status"], "exe": r["exe"], "verdict": None}


def by_key(con):
    """{key: {id, name, verdict, status, time}}: newest non-cancelled test per key (open ones are evaluated live)."""
    res = {}
    for r in con.execute("SELECT * FROM experiments WHERE key IS NOT NULL AND key!='' AND status!='cancelled' "
                         "ORDER BY id"):
        r = dict(r)
        v = evaluate(con, r)["verdict"] if r["status"] == "open" else (an._json(r["result"]) or {}).get("verdict")
        res[r["key"]] = {"id": r["id"], "name": r["name"], "verdict": v, "status": r["status"],
                         "time": an.fmt_ts(r["start_ts"])}
    return res


def overview(con):
    """GET /api/tests: all tests, the open one with its live result, and the game a new test would default to."""
    o = _open(con)
    return {"tests": list_all(con), "open": evaluate(con, o) if o else None, "default_exe": default_exe(con)}
