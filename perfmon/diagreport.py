"""Diagnostics extensions of the session report: extra stutter causes, report sections and findings.

Kept out of analyze.py (which only calls in here) so parallel branches touching analyze.py merge cheaply.
Human-readable strings are Polish, like the rest of the analysis."""
import bisect
import time

from . import analyze as an
from . import (bottleneck, config, crashinfo, diskinfo, disktemp, etwbox, kdumps, memdiag, memleak, present, procwatch,
               resetforensics)

VRAM_SHARED_MIN_MB = 512      # game's shared GPU memory above max(this, 5% of VRAM) = VRAM spills into RAM
VRAM_SHARED_JUMP_MB = 200     # ...or it grows this much within 10 s
BG_GPU_PCT = 15               # background process GPU load counted as a stutter cause
SHADER_GROWTH_BYTES = 256 * 1024   # shader cache growth around a stutter = it compiled shaders


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
        self.gpu = an._rows(con, "SELECT ts, name, gpu_pct FROM procs WHERE kind='gpu' AND gpu_pct<=? "
                            "AND ts BETWEEN ? AND ? ORDER BY ts", (config.GPU_PCT_MAX, start - 6, end + 6))
        self.gpu_ts = [p["ts"] for p in self.gpu]
        vt = con.execute("SELECT MAX(gpu_vram_total_mb) FROM samples WHERE ts BETWEEN ? AND ?", (start, end)).fetchone()[0]
        self.shared_limit = vram_shared_limit(vt)
        self.culprits = config.load().get("background_culprits")
        self.starts = procwatch.StartCauses(con, start, end, row["exe"], self.culprits)
        self.resets = [r[0] for r in con.execute("SELECT ts FROM incidents WHERE kind IN ('nvme_reset','disk_retry') "
                                                 "AND ts BETWEEN ? AND ? ORDER BY ts", (start - 15, end + 15))]
        self.disk_temp = disktemp.DiskTemp(con, row["path"] if "path" in row.keys() else None, start, end)
        # ETW dumps: wpr -stop flushes the buffer to disk and merges it, which stutters the game itself
        self.dumps = []
        for ts, trig, summ in con.execute("SELECT ts, trigger, summary FROM etw_captures WHERE ts BETWEEN ? AND ? "
                                          "ORDER BY ts", (start - 300, end)):
            stop_s = etwbox._loads(summ).get("stop_s")
            if stop_s is not None:
                self.dumps.append((ts, ts + stop_s + etwbox.DUMP_SETTLE_S, trig == "stutter_burst"))

    def _win(self, ts_list, items, a, b):
        return items[bisect.bisect_left(ts_list, a):bisect.bisect_right(ts_list, b)]

    def vram_shared(self, ts):
        near = [g for g in self._win(self.gp_ts, self.gp, ts - 5, ts + 5) if g["vram_shared_mb"] is not None]
        if any(g["vram_shared_mb"] > self.shared_limit for g in near):
            return True
        grow = [g for g in self._win(self.gp_ts, self.gp, ts - 10, ts + 5) if g["vram_shared_mb"] is not None]
        return any(b["vram_shared_mb"] - a["vram_shared_mb"] > VRAM_SHARED_JUMP_MB
                   for i, a in enumerate(grow) for b in grow[i + 1:] if b["ts"] - a["ts"] <= 10)

    def shader_compile(self, ts):
        """Shader cache grew > SHADER_GROWTH_BYTES between the last sample before and the last one after the stutter
        (window -10 s / +5 s: caches are rescanned every 10 s, game_proc rows come every 5 s)."""
        near = [g for g in self._win(self.gp_ts, self.gp, ts - 10, ts + 5) if g["shader_bytes"] is not None]
        before = [g["shader_bytes"] for g in near if g["ts"] <= ts]
        after = [g["shader_bytes"] for g in near if g["ts"] > ts]
        return bool(before and after) and after[-1] - before[-1] > SHADER_GROWTH_BYTES

    def background_gpu(self, ts):
        bg = {}
        for p in self._win(self.gpu_ts, self.gpu, ts - 5, ts + 5):
            n = (p["name"] or "").lower()
            if (p["gpu_pct"] or 0) > BG_GPU_PCT and n != self.exe and n not in an.NOT_BACKGROUND:
                bg[p["name"]] = max(bg.get(p["name"], 0), p["gpu_pct"])
        return sorted(bg, key=lambda k: -bg[k])[:2]

    def refine(self, ts, win, causes):
        """Add the extra causes to the list from analyze._causes_for_stutter (before its unknown fallback)."""
        if self._win(self.resets, self.resets, ts - 15, ts + 15):
            causes.insert(0, "disk_reset")
        # after the trigger only: the burst that caused the dump is the game's. A burst dump's disk flush and wpr CPU
        # would pass for disk/background causes, so it replaces them; other triggers fire on a problem that may still
        # be going on, so there it is one cause among the rest
        dumps = [burst for a, b, burst in self.dumps if a < ts <= b]
        if any(dumps):
            return ["etw_dump"]
        if dumps:
            causes.append("etw_dump")
        if any((s.get("pagefile_pct") or 0) > 60 and (s.get("hard_faults_s") or 0) > 200 for s in win):
            causes.append("pagefile")
        if self.disk_temp.hot(ts):
            causes.append("disk_hot")
        if self.vram_shared(ts):
            causes.append("vram_shared")
        causes += [f"background_gpu:{n}" for n in self.background_gpu(ts)]
        causes += self.starts.causes(ts)
        if self.shader_compile(ts):   # also keeps the shader_or_unknown fallback off
            causes.append("shader_compile")
        return causes


# ---------- report sections ----------
def game_proc_summary(con, sid):
    g = an._one(con, "SELECT COUNT(*) AS n, MAX(vram_dedicated_mb) AS ded_max, MAX(vram_shared_mb) AS shared_max, "
                     "AVG(CASE WHEN gpu_pct<=100 THEN gpu_pct END) AS gpu_avg, MAX(private_mb) AS private_max, MAX(rss_mb) AS rss_max, "
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


def first_session_after_change(con, row):
    """'driver' / 'game_update' when the previous session of this game ran another GPU driver / game build
    (both invalidate the shader caches), else 'none'; None for the first session of the game."""
    prev = an._one(con, "SELECT context FROM sessions WHERE lower(exe)=lower(?) AND start_ts<? "
                        "ORDER BY start_ts DESC LIMIT 1", (row["exe"], row["start_ts"]))
    if not prev:
        return None
    a, b = an._ctx(prev), an._ctx(row)
    if an._driver(a) and an._driver(b) and an._driver(a) != an._driver(b):
        return "driver"
    build = lambda c: ((c.get("steam") or {}).get("buildid"), c.get("game_version"))
    if any(x and y and x != y for x, y in zip(build(a), build(b))):
        return "game_update"
    return "none"


def shader_summary(con, row):
    r = con.execute("SELECT MIN(shader_bytes), MAX(shader_bytes) FROM game_proc WHERE session=? "
                    "AND shader_bytes IS NOT NULL", (row["id"],)).fetchone()
    return {"first_session_after_change": first_session_after_change(con, row),
            "cache_mb": an._r(r[1] / 2**20) if r and r[1] is not None else None,
            "cache_growth_mb": an._r((r[1] - r[0]) / 2**20, 2) if r and r[1] is not None else None}


def gpu_background(con, start, end, exe):
    n_ticks = con.execute("SELECT COUNT(DISTINCT ts) FROM procs WHERE kind='gpu' AND ts BETWEEN ? AND ?",
                          (start, end)).fetchone()[0]
    if not n_ticks:
        return []
    rows = an._rows(con, "SELECT name, SUM(gpu_pct) AS s, MAX(gpu_pct) AS mx, COUNT(*) AS n, AVG(vram_mb) AS vram "
                         "FROM procs WHERE kind='gpu' AND gpu_pct<=? AND ts BETWEEN ? AND ? "
                         "GROUP BY name ORDER BY s DESC LIMIT 12", (config.GPU_PCT_MAX, start, end))
    skip = an.NOT_BACKGROUND | {(exe or "").lower()}
    # avg over the whole session (a process missing from the top 5 at a tick counts as 0 there)
    return [{"name": r["name"], "avg_pct": an._r(r["s"] / n_ticks), "max_pct": an._r(r["mx"]),
             "seen_pct": an._r(100 * r["n"] / n_ticks), "vram_avg_mb": _i(r["vram"])}
            for r in rows if (r["name"] or "").lower() not in skip][:8]


def extend(con, rep, row, start, end):
    """Extra sections of session_report."""
    summ = rep.get("summary") or {}
    rep["crash"] = summ.get("crash") or (crashinfo.build(con, row["exe"], end, start, read_files=False)
                                         if row["end_reason"] == "crash" else None)
    rep["game_proc"] = game_proc_summary(con, row["id"])
    rep["disk"] = diskinfo.session_disk(con, start, end, row["path"], row["id"])
    rep["proc_events"] = procwatch.session_proc_events(con, start, end, row["exe"], config.load().get("background_culprits"))
    rep["gpu_background"] = gpu_background(con, start, end, row["exe"])
    rep["present"] = present.session_present(con, row, summ)
    rep["shader"] = shader_summary(con, row)
    rep["mem_growth"] = memleak.assess(con, row["id"], start, end)
    rep["memory"] = memdiag.session_memory(con, start, end)
    rep["disk_temp"] = disktemp.session_summary(con, row["path"], start, end)
    win = etwbox.settings(config.load())["dump_last_s"]
    for w in (rep.get("stutters") or {}).get("worst") or []:
        if "dpc" in w["causes"]:
            w["etw_capture"] = etwbox.nearest_capture(con, w["ts"], win)
    rep["bottleneck"] = bottleneck.compute(con, row["id"], start, end, (rep.get("hardware") or {}).get("vram_total_mb"))


_BOTTLENECK_CACHE = {}   # (db path, session id, end ts) -> kind; finished sessions do not change


def fill_bottleneck(con, metrics):
    """Bottleneck kind of finished sessions recorded before summary.bottleneck existed (comparisons)."""
    for s in metrics:
        if s.get("bottleneck") is None and s.get("end_ts"):
            key = (str(config.DB_PATH), s["id"], s["end_ts"])
            if key not in _BOTTLENECK_CACHE:
                b = bottleneck.compute(con, s["id"], s["start_ts"], s["end_ts"])
                _BOTTLENECK_CACHE[key] = b["kind"] if b else None
            s["bottleneck"] = _BOTTLENECK_CACHE[key]


def session_extras(con, rows):
    """{session id: {...}} extra per-session columns for session lists and comparisons (rows: sessions rows)."""
    pres = present.labels(con, [r["id"] for r in rows])
    out = {}
    for r in rows:
        summ = an._json(r.get("summary")) or {}
        out[r["id"]] = {"present": pres.get(r["id"]), "mem_growth": summ.get("mem_growth_mb_h"),
                        "bottleneck": (summ.get("bottleneck") or {}).get("kind")}
    return out


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
    if cc.get("pagefile", 0) >= 3:
        add("warning", f"{cc['pagefile']} stutterów przy intensywnym czytaniu z pliku wymiany",
            "Plik wymiany zajęty w >60% i >200 odczytów stron/s: brakuje RAM. Zamknij przeglądarkę/aplikacje w tle; "
            "plik wymiany zarządzany przez system na Samsung 990 PRO.")
    if cc.get("etw_dump", 0) >= 3:
        add("info", f"{cc['etw_dump']} stutterów w trakcie zapisu nagrania ETW przez NX Frame",
            "Zapis czarnej skrzynki ETW (wpr -stop) obciąża dysk i CPU, więc przycina grę. To koszt pomiaru, nie "
            "problem gry ani komputera; seria stutterów zapisuje najwyżej jedno nagranie na "
            f"{etwbox.settings(config.load())['burst_cooldown_s'] // 60} min.")
    sh = rep.get("shader") or {}
    change = {"driver": "sterownika GPU", "game_update": "aktualizacji gry"}.get(sh.get("first_session_after_change"))
    n_sh = cc.get("shader_compile", 0)
    if change and n_sh + cc.get("shader_or_unknown", 0) >= 3:
        add("info", f"Pierwsza sesja po zmianie {change}",
            f"Zmiana unieważnia cache shaderów, więc stuttery ({n_sh} przy rosnącym cache, "
            f"{cc.get('shader_or_unknown', 0)} bez innej przyczyny) to prawdopodobnie kompilacja shaderów. "
            "Powinny zniknąć w kolejnej sesji; jeśli zostaną - szukaj innej przyczyny.")
    elif n_sh >= 3:
        add("warning", f"{n_sh} stutterów przy kompilacji shaderów",
            f"Cache shaderów urósł o {sh.get('cache_growth_mb')} MB w trakcie sesji. Gra kompiluje shadery w locie: "
            "użyj prekompilacji w menu gry, nie czyść cache (Adrenalin -> Reset Shader Cache, Oczyszczanie dysku -> "
            "DirectX Shader Cache); jeśli dzieje się to w każdej sesji, cache jest kasowany.")
    f += memleak.finding(row["exe"], rep.get("mem_growth"))
    f += memdiag.findings(rep)
    f += disktemp.findings(rep)
    f += diskinfo.findings(rep)
    f += present.findings(rep)
    f += procwatch.findings(rep)
    bgc = {k.split(":", 1)[1]: v for k, v in cc.items() if k.startswith("background_gpu:")}
    heavy = [b for b in rep.get("gpu_background") or [] if (b["avg_pct"] or 0) >= 10 or (b["max_pct"] or 0) >= 30]
    if bgc or heavy:
        names = ", ".join(f"{b['name']} (śr. {b['avg_pct']}%, max {b['max_pct']}%)" for b in heavy[:4]) or \
            ", ".join(f"{k} ({v}x)" for k, v in bgc.items())
        add("warning", "Procesy w tle obciążające GPU",
            f"{names}. Przeglądarka z wideo, Wallpaper Engine, nagrywanie/overlaye zabierają GPU grze - zamknij je "
            "lub wyłącz akcelerację sprzętową na czas grania.")
    return f


def health_findings(con, rep):
    """Extra `pm health` findings: disk resets / health from incidents and the latest system_info."""
    f = []
    bk = rep.get("incidents_by_kind") or {}
    n = sum(bk.get(k, 0) for k in ("nvme_reset", "disk_retry", "disk_removed"))
    if n:
        f.append({"severity": "serious", "title": f"{n}x reset kontrolera / ponowienie I/O / odłączenie dysku",
                  "detail": "Zdarzenia stornvme 129 / disk 153 / disk 157 - firmware SSD, osadzenie M.2 / kabel SATA, "
                            "ASPM (Link State Power Management) w planie zasilania. Lista: pm incidents --kind nvme_reset"})
    r = con.execute("SELECT info FROM system_info ORDER BY ts DESC LIMIT 1").fetchone()
    for w in diskinfo.health_warnings(an._json(r[0]) if r else {}):
        f.append({"severity": "warning", "title": "Zdrowie dysku", "detail": w})
    f += resetforensics.findings(rep.get("unclean_shutdowns"))
    f += kdumps.findings(con, time.time() - float(rep.get("days") or 14) * 86400, rep.get("unclean_shutdowns"),
                         kdumps.latest_crash_dump(con))
    if bk.get("disk_health"):
        f.append({"severity": "serious", "title": f"{bk['disk_health']}x pogorszenie zdrowia dysku",
                  "detail": "Przybyło błędów nieskorygowanych lub zużycie nośnika przekroczyło 90% - kopia zapasowa, "
                            "sprawdź pm sysinfo (tabela zdrowia dysków)."})
    return f


def diag_markers(con, t0, t1):
    """Chart markers (timeline_markers(diag=True)): user marks, Defender scans and background culprits started
    during game sessions."""
    out = []
    for i in an.incidents_between(con, t0, t1, kinds=["user_mark", "defender_scan"], limit=5000):
        d = i["details"] if isinstance(i["details"], dict) else {}
        out.append({"ts": i["ts"], "end_ts": i["end_ts"], "kind": "mark" if i["kind"] == "user_mark" else "scan",
                    "label": (d.get("text") or "Znacznik") if i["kind"] == "user_mark" else i["title"], "session": None})
    culprits = {c.lower() for c in config.load().get("background_culprits") or procwatch.DEFAULT_CULPRITS}
    for r in con.execute("SELECT ts, name, session FROM proc_events WHERE action='start' AND session IS NOT NULL "
                         "AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 5000", (t0, t1)):
        if (r[1] or "").lower() in culprits:
            out.append({"ts": r[0], "end_ts": None, "kind": "start", "label": f"Start: {r[1]}", "session": r[2]})
    return out


DIAG_INCIDENTS = ["user_mark", "nvme_reset", "disk_retry", "disk_removed", "disk_health", "defender_scan", "malware",
                  "game_mem_leak", "etw_capture", "kernel_dump"]


def health_incidents(con, t0):
    """`pm health` / Problems list of the diagnostics incidents, newest first."""
    rows = an.incidents_between(con, t0, 4e9, kinds=DIAG_INCIDENTS, limit=100000)
    return sorted(rows, key=lambda i: -i["ts"])[:200]


def game_series(con, session, max_points=1500):
    """Game process series for the session chart: VRAM dedicated/shared, private memory (game_proc, every ~5 s)."""
    rows = con.execute("SELECT ts, vram_dedicated_mb, vram_shared_mb, private_mb FROM game_proc WHERE session=? "
                       "ORDER BY ts", (int(session),)).fetchall()
    step = max(1, -(-len(rows) // max(1, int(max_points))))
    rows = rows[::step]
    out = {"ts": [r[0] for r in rows]}
    for i, k in enumerate(("vram_dedicated_mb", "vram_shared_mb", "private_mb"), 1):
        out[k] = [round(r[i]) if r[i] is not None else None for r in rows]
    return out
