"""Which seconds of a session are real gameplay, and whether the session is good enough to compare.

Menus, loading screens, shader compilation and idle time distort FPS and stutter numbers, so comparisons and the
bottleneck use gameplay seconds only. Rules come from this PC's data: menus run at low GPU power or very high FPS,
cutscenes and AFK have no input, shader compilation pegs all cores."""
from collections import Counter

from . import framestats as fs

MIN_GAMEPLAY_S = 120     # below this a session says nothing about FPS (launcher restarts, 40 s sessions)
WARMUP_S = 30            # first seconds: shader warm-up, intro, loading
IDLE_S = 30              # no input this long = cutscene, AFK, match wait
MIN_GPU_SHARE = 0.6      # of the session's median GPU load: relative, a CPU-limited game runs at low GPU load all along
MIN_POWER_SHARE = 0.5    # of the session's p90 GPU power
MAX_CPU_TOTAL = 85       # every core busy = loading or shader compilation
MAX_FPS_X_MEDIAN = 3
GAP_S = 3                # a hitch can dip the load of its own second: short gaps inside gameplay stay gameplay


def _p90(vals):
    vals = sorted(vals)
    return vals[int(0.9 * (len(vals) - 1))] if vals else None


def select(frames, samples, start=None):
    """Subset of `frames` (dicts with ts, fps) that is gameplay; samples: dicts with ts, gpu_load, gpu_power_w,
    user_idle_s, cpu_total, fg_exe. Seconds without a sample are dropped."""
    frames = [f for f in frames if f["fps"]]
    if not frames:
        return []
    by_s = {int(s["ts"]): s for s in samples}
    fps = sorted(f["fps"] for f in frames)
    median = fps[len(fps) // 2]
    p90 = _p90([s["gpu_power_w"] for s in samples if s["gpu_power_w"]])
    loads = sorted(s["gpu_load"] for s in samples if s["gpu_load"] is not None)
    min_load = MIN_GPU_SHARE * loads[len(loads) // 2] if loads else 0
    # the game may hand over to a child process, so "the game" = what was in the foreground most of the time
    top = Counter(s["fg_exe"] for s in samples if s["fg_exe"]).most_common(1)
    game_fg = top[0][0] if top else None
    t0 = start if start is not None else frames[0]["ts"]
    out = []
    for f in frames:
        s = by_s.get(int(f["ts"]))
        if s is None or f["ts"] < t0 + WARMUP_S or f["fps"] > MAX_FPS_X_MEDIAN * median:
            continue
        if (s["user_idle_s"] or 0) > IDLE_S or (s["cpu_total"] or 0) > MAX_CPU_TOTAL:
            continue
        if (s["gpu_load"] or 0) < min_load or (game_fg and s["fg_exe"] != game_fg):
            continue
        if p90 and (s["gpu_power_w"] or 0) < MIN_POWER_SHARE * p90:
            continue
        out.append(f)
    return _fill_gaps(frames, out)


def _fill_gaps(frames, kept):
    ts = [int(f["ts"]) for f in kept]
    gaps = [(a, b) for a, b in zip(ts, ts[1:]) if 1 < b - a <= GAP_S + 1]
    fill = {t for a, b in gaps for t in range(a + 1, b)}
    keep = set(ts) | fill
    return [f for f in frames if int(f["ts"]) in keep]


def session_rows(con, sid, start, end):
    """(frames, samples) as dicts, the input of select(). fps / frames / ft_p99 are application frames (frame
    generation left out, old rows as recorded); disp_* the presented ones."""
    cols = {"ts": "ts", "fps": fs.APP_FPS, "frames": fs.APP_FRAMES, "ft_p99": fs.APP_FT_P99, "disp_fps": "fps",
            "disp_frames": "frames", "anim_err": "anim_err", "disp_ft_sd": "disp_ft_sd", "frame_gen_pct": "frame_gen_pct"}
    frames = [dict(zip(cols, r)) for r in con.execute(
        f"SELECT {', '.join(cols.values())} FROM frames WHERE session=? AND {fs.APP_FPS}>0 ORDER BY ts", (sid,))]
    skeys = ("ts", "gpu_load", "gpu_power_w", "user_idle_s", "cpu_total", "fg_exe")
    samples = [dict(zip(skeys, r)) for r in con.execute(
        f"SELECT {', '.join(skeys)} FROM samples WHERE ts BETWEEN ? AND ?", (start, end + 1))]
    return frames, samples


def stats(con, sid, start, end):
    """summary.gameplay: gameplay-only FPS / 1% low / stutters and whether the session is usable for comparisons.
    fps_avg / fps_1low count application frames (what comparisons use), fps_displayed_avg the presented ones;
    they differ only with frame generation (frame_gen_pct).
    None without frames (no PresentMon, pruned by retention): nothing to judge, not a bad session."""
    frames, samples = session_rows(con, sid, start, end)
    if not frames:
        return None
    keep = select(frames, samples, start)
    secs = len(keep)
    out = {"seconds": secs, "usable": secs >= MIN_GAMEPLAY_S, "share_pct": round(100 * secs / max(1.0, end - start), 1)}
    if not secs:
        return out
    kept = {int(f["ts"]) for f in keep}
    weight = [f["frames"] or f["fps"] for f in keep]
    p99 = [(f["ft_p99"], w) for f, w in zip(keep, weight) if f["ft_p99"]]
    stutters = sum(1 for (t,) in con.execute("SELECT ts FROM stutters WHERE session=?", (sid,)) if int(t) in kept)
    out.update(fps_avg=round(sum(weight) / sum(w / f["fps"] for f, w in zip(keep, weight)), 1),
               # frame-weighted mean of the per-second p99 frame time: comparable between sessions, not PresentMon's 1% low
               fps_1low=round(1000 * sum(w for _, w in p99) / sum(v * w for v, w in p99), 1) if p99 else None,
               stutters=stutters, stutters_per_min=round(60 * stutters / secs, 2))
    disp = [(f["disp_frames"], f["disp_fps"]) for f in keep if f["disp_frames"] and f["disp_fps"]]
    gen = [(f["frame_gen_pct"], f["disp_frames"] or 0) for f in keep if f["frame_gen_pct"] is not None]
    gw = sum(w for _, w in gen)
    out.update(fps_displayed_avg=round(sum(n for n, _ in disp) / sum(n / v for n, v in disp), 1) if disp else None,
               frame_gen_pct=round(sum(v * w for v, w in gen) / gw, 1) if gw else None,
               pacing=fs.pacing_rows([{"frames": f["disp_frames"], "fps": f["disp_fps"], "anim_err": f["anim_err"],
                                       "disp_ft_sd": f["disp_ft_sd"]} for f in keep]))
    return out
