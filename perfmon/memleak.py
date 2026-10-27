"""Game memory growth over a session: linear fit of the game's private bytes (commit) from game_proc.

A game whose commit keeps growing for the whole session leaks memory; late in long sessions that shows up as
paging, stutters and lower FPS. Human-readable strings are Polish, like the rest of the analysis."""
from . import analyze as an

MIN_SESSION_S = 30 * 60   # shorter sessions: loading and warm-up dominate the trend
SKIP_S = 5 * 60           # first minutes = loading the level, not a leak
LEAK_MB_H = 500
LEAK_R2 = 0.8
FPS_DROP = 0.85           # last-quarter FPS below this share of the 2nd quarter
MONO_BUCKETS = 8
MONO_TOLERANCE = 0.005    # a bucket median may dip this much (share) below the previous one


def fit(points):
    """Least squares over [(ts, mb)] -> (slope MB/s, r2) or None."""
    n = len(points)
    if n < 10:
        return None
    mx = sum(t for t, _ in points) / n
    my = sum(v for _, v in points) / n
    sxx = sum((t - mx) ** 2 for t, _ in points)
    syy = sum((v - my) ** 2 for _, v in points)
    if not sxx:
        return None
    sxy = sum((t - mx) * (v - my) for t, v in points)
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy else 0.0
    return slope, r2


def monotonic(points, buckets=MONO_BUCKETS):
    """Median of each time bucket never drops (within tolerance) and the last is above the first."""
    if len(points) < buckets * 3:
        return False
    size = len(points) // buckets
    meds = []
    for i in range(buckets):
        vals = sorted(v for _, v in points[i * size:(i + 1) * size])
        meds.append(vals[len(vals) // 2])
    return meds[-1] > meds[0] and all(b >= a * (1 - MONO_TOLERANCE) for a, b in zip(meds, meds[1:]))


def _fps(con, sid, t0, t1):
    r = con.execute("SELECT SUM(frames), SUM(frames/fps) FROM frames WHERE session=? AND fps>0 AND ts>=? AND ts<?",
                    (sid, t0, t1)).fetchone()
    return r[0] / r[1] if r and r[1] else None


def assess(con, sid, start, end):
    """{mem_growth_mb_h, mem_growth_r2, leak, ...} for sessions of at least 30 min with game_proc data, else None."""
    if not start or not end or end - start < MIN_SESSION_S:
        return None
    pts = [(t, v) for t, v in con.execute("SELECT ts, private_mb FROM game_proc WHERE session=? AND ts>=? AND "
                                          "private_mb IS NOT NULL ORDER BY ts", (sid, start + SKIP_S))]
    f = fit(pts)
    if not f:
        return None
    slope, r2 = f
    mb_h = slope * 3600
    q = (end - start) / 4
    fps_q2, fps_q4 = _fps(con, sid, start + q, start + 2 * q), _fps(con, sid, start + 3 * q, end + 1)
    mono = monotonic(pts)
    fps_drop = bool(fps_q2 and fps_q4 and fps_q4 < FPS_DROP * fps_q2)
    leak = (mb_h > LEAK_MB_H and r2 > LEAK_R2) or (mono and fps_drop)
    return {"mem_growth_mb_h": round(mb_h), "mem_growth_r2": round(r2, 2), "leak": leak, "monotonic": mono,
            "private_start_mb": round(pts[0][1]), "private_end_mb": round(pts[-1][1]),
            "fps_q2": an._r(fps_q2), "fps_q4": an._r(fps_q4)}


def title(exe, m):
    return f"{exe}: pamięć gry rośnie o {m['mem_growth_mb_h']} MB/h ({m['private_start_mb']} -> {m['private_end_mb']} MB)"


def finding(exe, m):
    if not m or not m.get("leak"):
        return []
    fps = (f" FPS spadł z {m['fps_q2']} (2. ćwiartka sesji) do {m['fps_q4']} (ostatnia)."
           if m.get("fps_q2") and m.get("fps_q4") and m["fps_q4"] < m["fps_q2"] else "")
    return [{"severity": "warning", "title": f"Możliwy wyciek pamięci gry ({m['mem_growth_mb_h']} MB/h)",
             "detail": f"Pamięć prywatna {exe} rosła przez całą sesję: {m['private_start_mb']} -> "
                       f"{m['private_end_mb']} MB (dopasowanie R² {m['mem_growth_r2']}).{fps} To błąd gry lub "
                       "moda/overlayu, nie sprzętu: restart gry co kilka godzin, aktualizacja gry, wyłączenie modów "
                       "i overlayów; zgłoś twórcom z tymi liczbami."}]
