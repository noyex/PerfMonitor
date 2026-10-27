"""Diagnostics extensions of the session report: extra stutter causes, report sections and findings.

Kept out of analyze.py (which only calls in here) so parallel branches touching analyze.py merge cheaply.
Human-readable strings are Polish, like the rest of the analysis."""
import bisect

from . import analyze as an

VRAM_SHARED_MIN_MB = 512      # game's shared GPU memory above max(this, 5% of VRAM) = VRAM spills into RAM
VRAM_SHARED_JUMP_MB = 200     # ...or it grows this much within 10 s
BG_GPU_PCT = 15               # background process GPU load counted as a stutter cause


def _i(v):
    return int(round(v)) if v is not None else None


def vram_shared_limit(vram_total_mb):
    return max(VRAM_SHARED_MIN_MB, 0.05 * (vram_total_mb or 0))


class StutterExt:
    """Per-session data for the extra stutter causes, loaded once per report."""

    def __init__(self, con, row, start, end):
        self.exe = (row["exe"] or "").lower()
        sid = row["id"]
        self.gp = an._rows(con, "SELECT ts, vram_shared_mb, shader_bytes FROM game_proc WHERE session=? ORDER BY ts", (sid,))
        self.gp_ts = [g["ts"] for g in self.gp]
        self.gpu = an._rows(con, "SELECT ts, name, gpu_pct FROM procs WHERE kind='gpu' AND ts BETWEEN ? AND ? ORDER BY ts",
                            (start - 6, end + 6))
        self.gpu_ts = [p["ts"] for p in self.gpu]
        vt = con.execute("SELECT MAX(gpu_vram_total_mb) FROM samples WHERE ts BETWEEN ? AND ?", (start, end)).fetchone()[0]
        self.shared_limit = vram_shared_limit(vt)

    def _win(self, ts_list, items, a, b):
        return items[bisect.bisect_left(ts_list, a):bisect.bisect_right(ts_list, b)]

    def vram_shared(self, ts):
        near = [g for g in self._win(self.gp_ts, self.gp, ts - 5, ts + 5) if g["vram_shared_mb"] is not None]
        if any(g["vram_shared_mb"] > self.shared_limit for g in near):
            return True
        grow = [g for g in self._win(self.gp_ts, self.gp, ts - 10, ts + 5) if g["vram_shared_mb"] is not None]
        return any(b["vram_shared_mb"] - a["vram_shared_mb"] > VRAM_SHARED_JUMP_MB
                   for i, a in enumerate(grow) for b in grow[i + 1:] if b["ts"] - a["ts"] <= 10)

    def background_gpu(self, ts):
        bg = {}
        for p in self._win(self.gpu_ts, self.gpu, ts - 5, ts + 5):
            n = (p["name"] or "").lower()
            if (p["gpu_pct"] or 0) > BG_GPU_PCT and n != self.exe and n not in an.NOT_BACKGROUND:
                bg[p["name"]] = max(bg.get(p["name"], 0), p["gpu_pct"])
        return sorted(bg, key=lambda k: -bg[k])[:2]

    def refine(self, ts, win, causes):
        """Add the extra causes to the list from analyze._causes_for_stutter (before its unknown fallback)."""
        if self.vram_shared(ts):
            causes.append("vram_shared")
        causes += [f"background_gpu:{n}" for n in self.background_gpu(ts)]
        return causes


# ---------- report sections ----------
def game_proc_summary(con, sid):
    g = an._one(con, "SELECT COUNT(*) AS n, MAX(vram_dedicated_mb) AS ded_max, MAX(vram_shared_mb) AS shared_max, "
                     "AVG(gpu_pct) AS gpu_avg, MAX(private_mb) AS private_max, MAX(rss_mb) AS rss_max, "
                     "AVG(io_read_mbs) AS io_avg, MAX(io_read_mbs) AS io_max, MAX(threads) AS threads_max, "
                     "MAX(handles) AS handles_max FROM game_proc WHERE session=?", (sid,))
    if not g or not g["n"]:
        return None
    th = [r[0] for r in con.execute("SELECT thread_max_pct FROM game_proc WHERE session=? AND thread_max_pct IS NOT NULL",
                                    (sid,))]
    return {"samples": g["n"], "vram_dedicated_max_mb": _i(g["ded_max"]), "vram_shared_max_mb": _i(g["shared_max"]),
            "gpu_pct_avg": an._r(g["gpu_avg"]), "private_max_mb": _i(g["private_max"]),
            "rss_max_mb": _i(g["rss_max"]), "io_read_avg_mbs": an._r(g["io_avg"], 2),
            "io_read_max_mbs": an._r(g["io_max"], 1), "thread_max_p95": an._r(an._pct(th, 0.95)),
            "threads_max": g["threads_max"], "handles_max": g["handles_max"]}


def gpu_background(con, start, end, exe):
    n_ticks = con.execute("SELECT COUNT(DISTINCT ts) FROM procs WHERE kind='gpu' AND ts BETWEEN ? AND ?",
                          (start, end)).fetchone()[0]
    if not n_ticks:
        return []
    rows = an._rows(con, "SELECT name, SUM(gpu_pct) AS s, MAX(gpu_pct) AS mx, COUNT(*) AS n, AVG(vram_mb) AS vram "
                         "FROM procs WHERE kind='gpu' AND ts BETWEEN ? AND ? GROUP BY name ORDER BY s DESC LIMIT 12",
                    (start, end))
    skip = an.NOT_BACKGROUND | {(exe or "").lower()}
    # avg over the whole session (a process missing from the top 5 at a tick counts as 0 there)
    return [{"name": r["name"], "avg_pct": an._r(r["s"] / n_ticks), "max_pct": an._r(r["mx"]),
             "seen_pct": an._r(100 * r["n"] / n_ticks), "vram_avg_mb": _i(r["vram"])}
            for r in rows if (r["name"] or "").lower() not in skip][:8]


def extend(con, rep, row, start, end):
    """Extra sections of session_report."""
    rep["game_proc"] = game_proc_summary(con, row["id"])
    rep["gpu_background"] = gpu_background(con, start, end, row["exe"])


# ---------- findings ----------
def findings(rep, row):
    f = []
    add = lambda sev, title, detail: f.append({"severity": sev, "title": title, "detail": detail})
    cc = (rep.get("stutters") or {}).get("cause_counts") or {}
    gp = rep.get("game_proc") or {}
    if cc.get("vram_shared", 0) >= 3:
        add("warning", f"{cc['vram_shared']} stutterów przy VRAM przelewającym się do RAM",
            f"Gra trzymała do {gp.get('vram_shared_max_mb')} MB w pamięci współdzielonej (system RAM). Obniż jakość "
            "tekstur / rozdzielczość tekstur, wyłącz pakiet tekstur HD - VRAM przelewa się do RAM.")
    bgc = {k.split(":", 1)[1]: v for k, v in cc.items() if k.startswith("background_gpu:")}
    heavy = [b for b in rep.get("gpu_background") or [] if (b["avg_pct"] or 0) >= 10 or (b["max_pct"] or 0) >= 30]
    if bgc or heavy:
        names = ", ".join(f"{b['name']} (śr. {b['avg_pct']}%, max {b['max_pct']}%)" for b in heavy[:4]) or \
            ", ".join(f"{k} ({v}x)" for k, v in bgc.items())
        add("warning", "Procesy w tle obciążające GPU",
            f"{names}. Przeglądarka z wideo, Wallpaper Engine, nagrywanie/overlaye zabierają GPU grze - zamknij je "
            "lub wyłącz akcelerację sprzętową na czas grania.")
    return f
