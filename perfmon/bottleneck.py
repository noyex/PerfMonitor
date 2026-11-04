"""What limits a game session and what would actually help: one `kind` with the reason and the advice.

Inputs: per-second frames (bound from PresentMon, or GPU load when PresentMon has no GPU timing), samples (GPU load,
power, clock) and game_proc (busiest game thread, shared VRAM). Human-readable strings are Polish."""
from . import analyze as an
from . import diagreport as dr
from . import gameplay

VRAM_SHARED_SHARE = 10      # % of game samples with shared VRAM above the spill limit
POWER_LIMITED_SHARE = 50    # % of GPU-busy seconds at the power limit with clocks pulled down
CPU_SHARE = 40
THREAD_P95 = 90
LIMIT_SHARE = 40
LIMIT_LOW_FPS = 20
GPU_SHARE = 60
GPU_LOAD_BOUND = 90         # GPU load counted as GPU-bound when PresentMon gives no GPUBusy (95 flipped CoD to CPU at 92%)

ADVICE = {
    "vram": "obniż jakość tekstur / rozdzielczość tekstur - VRAM przelewa się do RAM",
    "gpu_power": "GPU stoi na limicie mocy: undervolting albo wyższy limit mocy w Adrenalinie da realny zysk",
    "cpu_thread": "gra czeka na jeden wątek: pomoże wyższy zegar CPU, szybszy RAM/EXPO, Curve Optimizer; "
                  "więcej rdzeni nie",
    "cpu": "CPU nie nadąża: zamknij procesy w tle, sprawdź ustawienia obciążające CPU (tłum, fizyka, zasięg widzenia)",
    "limit": "ani CPU, ani GPU nie są w pełni zajęte: limiter FPS / vsync / sieć / dysk; tuning sprzętu nic nie da",
    "gpu": "GPU-bound: obniż ustawienia graficzne albo włącz upscaler (FSR / XeSS)",
    "mixed": "brak jednego dominującego ograniczenia",
}


def _pct(part, whole):
    return round(100 * part / whole, 1) if whole else None


def compute(con, sid, start, end, vram_total_mb=None):
    """{kind, why, advice, metrics} for a session, or None without frame data."""
    frames = an._rows(con, "SELECT ts, fps, ft_avg, cpu_busy, bound FROM frames WHERE session=? AND fps>0 ORDER BY ts",
                      (sid,))
    if not frames:
        return None
    samples = an._rows(con, "SELECT ts, gpu_load, gpu_power_w, gpu_clock_mhz, gpu_vram_total_mb, user_idle_s, cpu_total, "
                            "fg_exe FROM samples WHERE ts BETWEEN ? AND ?", (start, end + 1))
    played = gameplay.select(frames, samples, start)
    if len(played) >= gameplay.MIN_GAMEPLAY_S:   # menus and loading screens say nothing about the game's limit
        frames = played
    load_at = {int(s["ts"]): s["gpu_load"] for s in samples if s["gpu_load"] is not None}

    # bound per second; production PresentMon on this AMD card writes no GPUBusy, so fall back to GPU load
    source = "presentmon" if any(f["bound"] for f in frames) else "gpu_load"
    counts = {"GPU": 0, "CPU": 0, "limit": 0}
    fps_sorted = sorted(f["fps"] for f in frames)
    med = fps_sorted[len(fps_sorted) // 2]
    limit_low = 0
    for f in frames:
        b = f["bound"]
        if source == "gpu_load":
            load = load_at.get(int(f["ts"]), load_at.get(int(f["ts"]) - 1))
            if load is not None and load >= GPU_LOAD_BOUND:
                b = "GPU"
            elif f["cpu_busy"] is not None and f["ft_avg"] and f["cpu_busy"] >= 0.85 * f["ft_avg"]:
                b = "CPU"
            elif load is not None and f["cpu_busy"] is not None:
                b = "limit"
        if b in counts:
            counts[b] += 1
            if b == "limit" and f["fps"] < 0.8 * med:
                limit_low += 1
    nb = sum(counts.values())
    shares = {k: _pct(v, nb) or 0.0 for k, v in counts.items()}

    busy = [s for s in samples if (s["gpu_load"] or 0) > 90]
    powers = [s["gpu_power_w"] for s in samples if s["gpu_power_w"]]
    clocks = [s["gpu_clock_mhz"] for s in busy if s["gpu_clock_mhz"]]
    power_limited = None
    if busy and powers and clocks:
        p99, c90 = an._pct(powers, 0.99), an._pct(clocks, 0.90)
        n = sum(1 for s in busy if (s["gpu_power_w"] or 0) >= 0.97 * p99 and s["gpu_clock_mhz"]
                and s["gpu_clock_mhz"] < 0.95 * c90)
        power_limited = _pct(n, len(busy))

    gp = an._rows(con, "SELECT thread_max_pct, vram_shared_mb FROM game_proc WHERE session=?", (sid,))
    threads = [g["thread_max_pct"] for g in gp if g["thread_max_pct"] is not None]
    shared = [g["vram_shared_mb"] for g in gp if g["vram_shared_mb"] is not None]
    if vram_total_mb is None:
        vram_total_mb = max((s["gpu_vram_total_mb"] or 0 for s in samples), default=0)
    lim = dr.vram_shared_limit(vram_total_mb)
    m = {
        "bound_source": source, "gpu_bound_pct": shares["GPU"], "cpu_bound_pct": shares["CPU"],
        "limit_pct": shares["limit"], "gpu_power_limited_pct": power_limited,
        "game_thread_p95": an._r(an._pct(threads, 0.95)) if threads else None,
        "vram_shared_max_mb": round(max(shared)) if shared else None,
        "vram_shared_over_pct": _pct(sum(1 for v in shared if v > lim), len(shared)) if shared else None,
        "limit_low_fps_pct": _pct(limit_low, len(frames)),
    }
    kind, why = classify(m)
    return {"kind": kind, "why": why, "advice": ADVICE[kind], "metrics": m}


def classify(m):
    """First matching kind and the reason (Polish)."""
    g, c, lim = m["gpu_bound_pct"], m["cpu_bound_pct"], m["limit_pct"]
    if (m["vram_shared_over_pct"] or 0) >= VRAM_SHARED_SHARE:
        return "vram", (f"VRAM gry przelewa się do RAM przez {m['vram_shared_over_pct']}% sesji "
                        f"(shared max {m['vram_shared_max_mb']} MB)")
    # power limit only matters when the GPU is what the game waits for
    if (m["gpu_power_limited_pct"] or 0) >= POWER_LIMITED_SHARE and g >= CPU_SHARE:
        return "gpu_power", (f"GPU-bound {g}% czasu, z tego {m['gpu_power_limited_pct']}% na limicie mocy "
                             "z obniżonym zegarem")
    if c >= CPU_SHARE and (m["game_thread_p95"] or 0) >= THREAD_P95:
        return "cpu_thread", f"CPU-bound {c}% czasu, najbardziej obciążony wątek gry p95 {m['game_thread_p95']}%"
    if c >= CPU_SHARE:
        return "cpu", f"CPU-bound {c}% czasu"
    if lim >= LIMIT_SHARE and (m["limit_low_fps_pct"] or 0) >= LIMIT_LOW_FPS:
        return "limit", (f"limit {lim}% czasu, w {m['limit_low_fps_pct']}% sekund FPS poniżej 80% mediany "
                         "przy wolnym CPU i GPU")
    if g >= GPU_SHARE:
        return "gpu", f"GPU-bound {g}% czasu"
    return "mixed", f"GPU {g}% / CPU {c}% / limit {lim}%"


def refresh_all(con, merge_summary):
    """Recompute stored bottlenecks and gameplay stats of finished sessions with the current rules."""
    rows = con.execute(f"SELECT id, start_ts, end_ts FROM sessions WHERE end_ts IS NOT NULL AND {an.GAMES_ONLY}").fetchall()
    for sid, start, end in rows:
        gp = gameplay.stats(con, sid, start, end)
        extra = {"gameplay": gp} if gp else {}
        b = compute(con, sid, start, end)
        if b:
            extra["bottleneck"] = {"kind": b["kind"], "why": b["why"], **b["metrics"]}
        merge_summary(sid, extra)
    return len(rows)


def describe(b):
    return f"{b['kind']} ({b['why']}) -> {b['advice']}" if b else None
