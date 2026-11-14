"""Frame-level judgements shared by the capture and the analysis: per-second bound, which side a stutter came from,
frame pacing, and the SQL that reads application frames (frame generation excluded) with a fallback for rows
recorded before those columns existed."""
from . import stuttercause

BUSY_BOUND = 0.85      # busy share of the application frametime that makes a second CPU- or GPU-bound
STUTTER_BUSY = 0.5     # busy share of a stutter frame that puts it on that side

# Application frames per second. Rows from before app_frames existed are scaled by their frame_gen_pct, so old
# sessions with frame generation compare with new ones (exact for FPS; the p99 frametime is an estimate) and rows
# without frame type data stay as recorded. A second with only generated frames has app_fps NULL (fps>0 skips it).
_NATIVE = "(1 - COALESCE(frame_gen_pct, 0) / 100.0)"
APP_FPS = f"(CASE WHEN app_frames IS NULL THEN fps * {_NATIVE} ELSE app_fps END)"
APP_FRAMES = f"COALESCE(app_frames, frames * {_NATIVE})"
APP_FT_P99 = f"(CASE WHEN app_frames IS NULL THEN ft_p99 / {_NATIVE} ELSE app_ft_p99 END)"
APP_FT_AVG = f"(CASE WHEN app_frames IS NULL THEN ft_avg / {_NATIVE} WHEN app_fps>0 THEN 1000.0/app_fps END)"
# old 2.x rows averaged the generated frames' MsCPUBusy of 0 in
APP_CPU_BUSY = f"(CASE WHEN app_frames IS NULL THEN cpu_busy / {_NATIVE} ELSE cpu_busy END)"

SIDE_LABELS = {"cpu": "po stronie CPU", "gpu": "po stronie GPU", "present": "w prezentacji / kolejce klatek"}


def bound(app_ft, cpu_busy, gpu_busy):
    """GPU / CPU / limit for one second from the mean busy times of its application frames, None without timing.
    An in-game FPS limiter sleeps inside the frame and PresentMon counts that as CPU busy: a capped game can read
    CPU-bound (the bottleneck's limit rule looks at FPS drops instead)."""
    if cpu_busy is None or gpu_busy is None or not app_ft or app_ft <= 0:
        return None
    if gpu_busy >= BUSY_BOUND * app_ft:
        return "GPU"
    if cpu_busy >= BUSY_BOUND * app_ft:
        return "CPU"
    return "limit"   # neither CPU nor GPU is busy the full time: FPS limit / vsync / waiting


def stutter_side(ft, cpu_busy, gpu_busy):
    """cpu / gpu: that processor was busy for most of the long frame; present: neither was, the time went to
    waiting (present queue, vsync, DWM, driver, frame generation pacing). None without timing (old rows).
    A generated frame carrying the gap does not make it 'present': interpolation waits for the next real frame,
    so a slow application frame shows up as a late generated one. With one of the two missing only the known side
    can be confirmed: the missing one may hold the time."""
    if not ft:
        return None
    c, g = cpu_busy or 0.0, gpu_busy or 0.0
    if max(c, g) >= STUTTER_BUSY * ft:
        return "cpu" if c >= g else "gpu"
    return "present" if cpu_busy is not None and gpu_busy is not None else None


def pacing(anim_sum, anim_n, jit_sum, jit_ft_sum, jit_n):
    """{anim_err_ms, disp_jitter_ms, disp_jitter_pct} or None.
    anim_err_ms: mean |MsAnimationError|, how far the shown motion is off the time between frames (judder even at a
    high FPS). disp_jitter_*: frame-weighted mean of the per-second standard deviation of the displayed frametime,
    in ms and % of the mean displayed frametime; per second, so FPS changes over the session do not count."""
    out = {"anim_err_ms": round(anim_sum / anim_n, 2) if anim_n else None,
           "disp_jitter_ms": round(jit_sum / jit_n, 2) if jit_n else None,
           "disp_jitter_pct": round(100 * jit_sum / jit_ft_sum, 1) if jit_n and jit_ft_sum else None}
    return out if any(v is not None for v in out.values()) else None


def pacing_rows(rows):
    """pacing() from per-second frames rows (dicts with frames, fps, anim_err, disp_ft_sd); the capture adds up the
    same per-second values with the same weights."""
    a = an = j = jf = jn = 0.0
    for r in rows:
        n = r["frames"] or 0
        if r["anim_err"] is not None:
            a, an = a + r["anim_err"] * n, an + n
        if r["disp_ft_sd"] is not None and r["fps"]:
            j, jf, jn = j + r["disp_ft_sd"] * n, jf + n * 1000.0 / r["fps"], jn + n
    return pacing(a, an, j, jf, jn)


def session_pacing(con, sid):
    rows = [dict(zip(("frames", "fps", "anim_err", "disp_ft_sd"), r)) for r in con.execute(
        "SELECT frames, fps, anim_err, disp_ft_sd FROM frames WHERE session=? AND fps>0", (sid,))]
    return pacing_rows(rows)


def side_ranking(stutters, base_ts, side_at):
    """stuttercause.rank of stutter sides against the sides of quiet gameplay seconds (side_at: {second: the side
    stutter_side() gives that second's mean frame}): in a GPU-bound game GPU-side stutters are the normal state,
    a CPU-side one is the anomaly. None without frame timing."""
    st = [[s["side"]] for s in stutters if s.get("side")]
    base = [[side_at[int(t)]] for t in base_ts if side_at.get(int(t))]
    if not st or not base:
        return None
    return stuttercause.rank(st, base)
