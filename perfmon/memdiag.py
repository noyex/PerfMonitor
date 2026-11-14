"""Who holds the commit and could the pagefile grow: memory lists, per-process private bytes, pagefile headroom.

Collector side: mem_row() (one mem_samples row from the PDH memory counters), pagefile_status(), commit_details()
for commit_full incidents. Analysis side: session_memory() for the session report and findings() naming the
processes holding the commit. Commit = private bytes; RSS (working set) says nothing about it: a process can hold
GBs of commit with a small working set (pages trimmed to the standby/modified lists or the pagefile).
Human-readable strings are Polish."""
import os
import winreg

import psutil

from . import analyze as an

MM_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
COMMIT_FULL = 0.92        # commit / limit above this = commit_full (collector.detect_live)
TOP_COMMIT = 5            # procs kind='commit' rows per tick (only processes not already in the cpu/mem/io rows)
MEM_EVERY_S = 30          # mem_samples cadence outside game/stress sessions (every procs tick inside them)
PF_FREE_RESERVE_GB = 1.0  # Windows does not grow a pagefile into the last bit of free space
COMPRESSION_PROCS = ("MemCompression", "Memory Compression")


def _mb(v):
    return round(v / 2**20, 1) if v is not None else None


# ---------- collector ----------
def _paging_files():
    """[(path or None for "every volume", min MB, max MB)] from PagingFiles; auto = system-managed."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MM_KEY) as k:
            entries = winreg.QueryValueEx(k, "PagingFiles")[0]
            try:
                existing = winreg.QueryValueEx(k, "ExistingPageFiles")[0]
            except OSError:
                existing = []
    except OSError:
        return [], []
    out = []
    for e in entries or []:
        parts = e.split()
        if not parts:
            continue
        path = parts[0]
        lo = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        hi = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        out.append((None if path.startswith("?") else path, lo, hi))
    return out, [p.replace("\\??\\", "") for p in existing or []]


def system_managed_max_gb(ram_gb, volume_gb):
    """Windows' cap for a system-managed pagefile: 3x RAM or 4 GB, whichever is larger, at most 1/8 of the volume."""
    return min(max(3 * ram_gb, 4.0), volume_gb / 8)


def pagefile_status():
    """{"auto", "files": [{path, size_gb, max_gb, volume_free_gb, can_grow_gb}], "size_gb", "can_grow_gb"}.
    can_grow_gb is an upper bound: free space minus a reserve, never above the configured / system-managed maximum."""
    entries, existing = _paging_files()
    ram_gb = psutil.virtual_memory().total / 2**30
    auto = any(p is None for p, _, _ in entries)
    cfg = {p.lower(): (lo, hi) for p, lo, hi in entries if p}
    files = []
    for path in existing or [p for p in cfg]:
        try:
            size = os.stat(path).st_size / 2**30
            du = psutil.disk_usage(path[:3])
        except OSError:
            continue
        lo, hi = cfg.get(path.lower(), (0, 0))
        managed = auto or (lo == 0 and hi == 0)
        mx = system_managed_max_gb(ram_gb, du.total / 2**30) if managed else hi / 1024
        free = du.free / 2**30
        grow = max(0.0, min(mx - size, free - PF_FREE_RESERVE_GB))
        files.append({"path": path, "size_gb": round(size, 1), "max_gb": round(mx, 1), "system_managed": managed,
                      "volume_free_gb": round(free, 1), "can_grow_gb": round(grow, 1)})
    return {"auto": auto, "files": files, "size_gb": round(sum(f["size_gb"] for f in files), 1),
            "max_gb": round(sum(f["max_gb"] for f in files), 1),
            "free_gb": round(sum(f["volume_free_gb"] for f in files), 1),
            "can_grow_gb": round(sum(f["can_grow_gb"] for f in files), 1)}


def lists(pdh):
    """Memory lists (MB) from a PdhCounters.read() dict."""
    sb = [pdh.get(k) for k in ("standby_core", "standby_normal", "standby_reserve")]
    return {"standby_mb": _mb(sum(v for v in sb if v is not None)) if any(v is not None for v in sb) else None,
            "modified_mb": _mb(pdh.get("modified_bytes")), "free_mb": _mb(pdh.get("free_zero_bytes")),
            "pool_np_mb": _mb(pdh.get("pool_np_bytes"))}


def mem_row(ts, session, pdh, compressed, pf):
    has = bool(pf and pf["files"])
    return dict(ts=ts, session=session, **lists(pdh), compressed_mb=compressed,
                pf_mb=round(pf["size_gb"] * 1024) if has else None,
                pf_max_mb=round(pf["max_gb"] * 1024) if has else None,
                pf_free_gb=pf["free_gb"] if has else None,
                pf_grow_mb=round(pf["can_grow_gb"] * 1024) if has else None,
                pf_drives=" ".join(sorted({f["path"][:2].upper() for f in pf["files"]})) if has else None)


def top_commit(rows, n=8):
    """Processes holding the most commit (private bytes) from the collector's last procs tick: walking the process
    list again here costs ~0.5 s. rows: (name, pid, cpu %, rss MB, io MB/s, private MB)."""
    return [{"name": r[0], "pid": r[1], "commit_mb": round(r[5]), "rss_mb": round(r[3])}
            for r in sorted(rows, key=lambda x: -x[5])[:n]]


def commit_details(pdh, top, compressed=None):
    """Extra details of a commit_full incident: commit holders, memory lists, pagefile headroom."""
    pf = pagefile_status()
    return {"top_commit": top, "mem": dict(lists(pdh), compressed_mb=compressed), "pagefile": pf,
            "pagefile_can_grow": pf["can_grow_gb"] >= 1 if pf["files"] else None}


# ---------- analysis ----------
def commit_holders(con, t0, t1, n=6):
    """Apps holding the most private bytes over [t0, t1]: per procs tick the sum over the app's recorded processes
    (a browser is 30 processes; only the top ones of each tick are stored, so this is a lower bound), max over ticks.
    A pid appears in several kinds of rows per tick (cpu/mem/gpu): DISTINCT counts it once."""
    rows = an._rows(con, "SELECT name, MAX(s) AS mx, AVG(s) AS avg, MAX(n) AS procs FROM ("
                         "SELECT ts, name, SUM(commit_mb) AS s, COUNT(*) AS n FROM ("
                         "SELECT DISTINCT ts, name, pid, commit_mb FROM procs WHERE commit_mb IS NOT NULL AND ts BETWEEN ? AND ?"
                         ") GROUP BY ts, name) GROUP BY name ORDER BY mx DESC LIMIT ?", (t0, t1, n))
    return [{"name": r["name"], "commit_max_mb": round(r["mx"]), "commit_avg_mb": round(r["avg"]), "procs_max": r["procs"]}
            for r in rows]


def session_memory(con, start, end):
    """Report section: commit peak vs limit, commit holders (game first), memory lists and pagefile headroom."""
    s = an._one(con, "SELECT MAX(commit_gb) AS cmax, MAX(commit_limit_gb) AS lim, MAX(ram_pct) AS ram FROM samples "
                     "WHERE ts BETWEEN ? AND ?", (start, end))
    m = an._one(con, "SELECT COUNT(*) AS n, MAX(standby_mb) AS sb_max, MIN(standby_mb) AS sb_min, "
                     "MAX(modified_mb) AS mod_max, MIN(free_mb) AS free_min, MAX(compressed_mb) AS comp_max, "
                     "MAX(pool_np_mb) AS np_max, MAX(pf_mb) AS pf_max, MIN(pf_free_gb) AS pf_free_min, "
                     "MIN(pf_grow_mb) AS pf_grow_min, MAX(pf_max_mb) AS pf_cap, MAX(pf_drives) AS pf_drives "
                     "FROM mem_samples WHERE ts BETWEEN ? AND ?", (start, end))
    holders = commit_holders(con, start, end)
    if not (s and s["cmax"]) and not holders and not (m and m["n"]):
        return None
    out = {"commit_peak_gb": an._r(s["cmax"]), "commit_limit_gb": an._r(s["lim"]),
           "commit_peak_pct": an._r(100 * s["cmax"] / s["lim"]) if s["cmax"] and s["lim"] else None,
           "ram_peak_pct": an._r(s["ram"]), "commit_holders": holders}
    if m and m["n"]:
        out.update(standby_min_mb=_i(m["sb_min"]), standby_max_mb=_i(m["sb_max"]), modified_max_mb=_i(m["mod_max"]),
                   free_min_mb=_i(m["free_min"]), compressed_max_mb=_i(m["comp_max"]), pool_np_max_mb=_i(m["np_max"]),
                   pagefile_max_mb=_i(m["pf_max"]), pagefile_volume_free_min_gb=an._r(m["pf_free_min"]),
                   pagefile_can_grow_min_mb=_i(m["pf_grow_min"]), pagefile_cap_mb=_i(m["pf_cap"]),
                   pagefile_drives=m["pf_drives"])
    return out


def _i(v):
    return int(round(v)) if v is not None else None


def _holders_txt(holders, n=4):
    return ", ".join(f"{h['name']}{f' ({p} proc.)' if (p := h.get('procs_max') or 1) > 1 else ''} "
                     f"{h['commit_max_mb'] / 1024:.1f} GB" for h in holders[:n])


def pagefile_txt(mem):
    """Can the pagefile grow? From session_memory() / mem_samples (None when it was not measured)."""
    grow, free = mem.get("pagefile_can_grow_min_mb"), mem.get("pagefile_volume_free_min_gb")
    if grow is None:
        return None
    drive = mem.get("pagefile_drives") or "dysku z plikiem wymiany"
    size, cap = mem.get("pagefile_max_mb"), mem.get("pagefile_cap_mb")
    if grow < 1024 and size and cap and size >= cap - 1024:
        return (f"Plik wymiany doszedł do swojego maksimum ({cap / 1024:.0f} GB): limit commit nie może się zwiększyć - "
                "podnieś rozmiar maksymalny pliku wymiany albo przełącz go na zarządzany przez system.")
    if grow < 1024:
        return (f"Plik wymiany nie ma miejsca, by urosnąć (wolne na {drive} min. {free} GB): limit commit nie może "
                f"się zwiększyć - zwolnij miejsce na {drive} albo ustaw plik wymiany na dysku z wolnym miejscem.")
    return (f"Plik wymiany mógł jeszcze urosnąć o ok. {grow / 1024:.0f} GB (wolne na jego dysku min. {free} GB), ale rośnie "
            "dopiero po dojściu do limitu i to chwilę trwa - stały, większy plik wymiany (np. 16-32 GB) usuwa to opóźnienie.")


def findings(rep):
    """Commit findings for the session report: who holds it and whether the pagefile could grow."""
    mem = rep.get("memory") or {}
    cc = (rep.get("stutters") or {}).get("cause_counts") or {}
    n = cc.get("ram", 0) + cc.get("pagefile", 0)
    pct = mem.get("commit_peak_pct") or 0
    if not mem or (n < 3 and pct < 100 * COMMIT_FULL):
        return []
    parts = []
    if mem.get("commit_holders"):
        parts.append(f"Najwięcej pamięci zadeklarowanej (commit) trzymały: {_holders_txt(mem['commit_holders'])}.")
    if mem.get("compressed_max_mb") and mem["compressed_max_mb"] >= 512:
        parts.append(f"Windows kompresował pamięć (do {mem['compressed_max_mb'] / 1024:.1f} GB w magazynie kompresji) - "
                     "to kosztuje czas CPU w trakcie gry.")
    pf = pagefile_txt(mem)
    if pf:
        parts.append(pf)
    title = (f"Commit do {pct:.0f}% limitu ({mem.get('commit_peak_gb')}/{mem.get('commit_limit_gb')} GB)" if pct
             else "Pamięć zadeklarowana (commit) blisko limitu")
    return [{"severity": "warning", "title": title, "detail": " ".join(parts) or
             "Brak danych o procesach (kolumna commit_mb zbierana od tej wersji)."}]
