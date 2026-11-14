"""UV/OC: profile registry, per-session aggregates, profile comparison, V/F curves, per-core view, stability.

Pure functions over a sqlite connection (the write helpers commit themselves). Used by the collector
(session_stats, active_profile_id), the CLI (`pm oc`, `pm stress`) and the UI API. PerfMonitor never
changes hardware settings: profiles only describe what the user set in BIOS / Adrenalin."""
import json
import time
from collections import defaultdict

from . import framestats as fs
from .analyze import _eff_ends, _json, _pct, _r, _rows, fmt_ts, parse_time

STATUSES = ("testing", "stable", "rejected")
CPU_KEYS = {"co_per_core", "pbo", "expo", "soc_v"}
PBO_KEYS = {"ppt", "tdc", "edc", "scalar", "boost_override"}
GPU_KEYS = {"voltage_offset_mv", "power_limit_pct", "max_clock_offset", "vram_mhz", "vram_fast_timing", "fan_curve"}
ERROR_KINDS = ("whea_corrected", "whea_fatal", "whea", "gpu_driver", "unclean_shutdown", "bsod")
STRESS_KINDS = ("stress", "manual", "idle_soak")

# "stable" requires all of these (plan section 10.3)
STABLE_GAME_H = 10
STABLE_WORK_DAYS = 3
STABLE_TESTS = {           # test name -> minimum minutes without errors
    "CoreCycler": 8 * 60,  # ~1 h per core on the 7700X
    "y-cruncher": 60,
    "OCCT": 60,
    "3DMark": 20,          # Steel Nomad stress test
}
MIN_COMPARE_MIN = 30       # below this the profile comparison warns about a small sample
LOW_LOAD_CPU = 15          # % CPU before a reset = "reset at low load" (typical of a too aggressive CO)
FULL_LOAD = 95             # % thread load for "core fully loaded"


# --------------------------------------------------------------------------
# profiles (write helpers)
# --------------------------------------------------------------------------

def _clean(d, allowed, what):
    if d is None:
        return None
    if not isinstance(d, dict):
        raise ValueError(f"{what}: oczekiwano obiektu JSON")
    bad = set(d) - allowed
    if bad:
        raise ValueError(f"{what}: nieznane pola {sorted(bad)} (dozwolone: {sorted(allowed)})")
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _num(d, key, lo, hi, msg):
    if key not in d:
        return
    try:
        v = float(d[key])
    except (TypeError, ValueError):
        raise ValueError(f"{key}: oczekiwano liczby") from None
    if not lo <= v <= hi:
        raise ValueError(msg)


def validate(cpu=None, gpu=None):
    cpu = _clean(cpu, CPU_KEYS, "cpu")
    gpu = _clean(gpu, GPU_KEYS, "gpu")
    if cpu:
        co = cpu.get("co_per_core")
        if co is not None:
            if not isinstance(co, list) or not all(isinstance(x, (int, float)) and -60 <= x <= 30 for x in co):
                raise ValueError("co_per_core: lista liczb z zakresu -60..30 (po jednej na rdzeń)")
            cpu["co_per_core"] = [int(x) for x in co]
        if "pbo" in cpu:
            cpu["pbo"] = _clean(cpu["pbo"], PBO_KEYS, "cpu.pbo")
        _num(cpu, "soc_v", 0.8, 1.35, "soc_v: 0.8..1.35 V (powyżej 1.30 V ryzyko uszkodzenia Zen 4)")
    if gpu:
        _num(gpu, "voltage_offset_mv", -300, 50, "voltage_offset_mv: -300..50")
        _num(gpu, "power_limit_pct", -50, 50, "power_limit_pct: -50..50")
        for k in ("max_clock_offset", "vram_mhz"):
            _num(gpu, k, -2000, 5000, f"{k}: poza zakresem")
    return cpu, gpu


def _merge(old, new):
    out = dict(old or {})
    for k, v in (new or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def create_profile(con, name, cpu=None, gpu=None, notes=None, status="testing"):
    if not name or not str(name).strip():
        raise ValueError("Profil musi mieć nazwę")
    if status not in STATUSES:
        raise ValueError(f"status: {'|'.join(STATUSES)}")
    cpu, gpu = validate(cpu, gpu)
    cur = con.execute("INSERT INTO oc_profiles(name, created_ts, status, cpu, gpu, notes) VALUES (?,?,?,?,?,?)",
                      (str(name).strip(), time.time(), status, json.dumps(cpu or {}), json.dumps(gpu or {}), notes))
    con.commit()
    return cur.lastrowid


def update_profile(con, pid, name=None, cpu=None, gpu=None, notes=None, status=None, replace=False):
    """cpu/gpu None = keep. replace=False merges the given keys (CLI flags); replace=True takes the dicts as the
    complete new settings (UI editor), so cleared fields are removed."""
    p = profile(con, pid)
    if not p:
        raise ValueError(f"Nie ma profilu #{pid}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"status: {'|'.join(STATUSES)}")
    cpu, gpu = validate(cpu, gpu)
    con.execute("UPDATE oc_profiles SET name=?, status=?, cpu=?, gpu=?, notes=? WHERE id=?",
                ((name or p["name"]).strip(), status or p["status"],
                 json.dumps(p["cpu"] if cpu is None else cpu if replace else _merge(p["cpu"], cpu)),
                 json.dumps(p["gpu"] if gpu is None else gpu if replace else _merge(p["gpu"], gpu)),
                 p["notes"] if notes is None else notes, int(pid)))
    con.commit()


def activate(con, pid, ts=None, source="manual"):
    """Profile active from ts (default now) until the next activation; re-tags sessions started since."""
    if not profile(con, pid):
        raise ValueError(f"Nie ma profilu #{pid}")
    ts = time.time() if ts is None else float(ts)
    last = con.execute("SELECT MAX(ts) FROM oc_profile_log").fetchone()[0]
    if last is not None and ts < last:
        raise ValueError(f"Aktywację można cofnąć najwyżej do ostatniej zmiany profilu ({fmt_ts(last)})")
    con.execute("INSERT INTO oc_profile_log(ts, profile_id, source) VALUES (?,?,?)", (ts, int(pid), source))
    retag_sessions(con)
    con.commit()
    return ts


def retag_sessions(con):
    """sessions.profile_id = profile active at each session's start (after a back-dated activation)."""
    log = [(r["ts"], r["profile_id"]) for r in con.execute("SELECT ts, profile_id FROM oc_profile_log ORDER BY ts")]
    if not log:
        return
    first = log[0][0]
    for r in con.execute("SELECT id, start_ts FROM sessions WHERE start_ts>=?", (first,)).fetchall():
        pid = None
        for ts, p in log:
            if ts <= r["start_ts"]:
                pid = p
            else:
                break
        con.execute("UPDATE sessions SET profile_id=? WHERE id=?", (pid, r["id"]))
        con.execute("UPDATE oc_session_stats SET profile_id=? WHERE session=?", (pid, r["id"]))


# --------------------------------------------------------------------------
# profiles (read)
# --------------------------------------------------------------------------

def _profile_row(r):
    d = dict(r)
    d["cpu"], d["gpu"] = _json(d.get("cpu")) or {}, _json(d.get("gpu")) or {}
    d["created"] = fmt_ts(d["created_ts"])
    return d


def profile(con, pid):
    r = con.execute("SELECT * FROM oc_profiles WHERE id=?", (int(pid),)).fetchone()
    return _profile_row(r) if r else None


def active_profile_id(con, ts=None):
    r = con.execute("SELECT profile_id FROM oc_profile_log WHERE ts<=? ORDER BY ts DESC LIMIT 1",
                    (time.time() if ts is None else ts,)).fetchone()
    return r[0] if r else None


def periods(con):
    """[(profile_id, from_ts, to_ts)] activity periods; the last one runs until now."""
    log = [(r["ts"], r["profile_id"]) for r in con.execute("SELECT ts, profile_id FROM oc_profile_log ORDER BY ts")]
    now = time.time()
    return [(p, ts, log[i + 1][0] if i + 1 < len(log) else now) for i, (ts, p) in enumerate(log)]


def _profile_periods(con, pid):
    return [(a, b) for p, a, b in periods(con) if p == pid]


def profiles(con):
    act = active_profile_id(con)
    per = defaultdict(list)
    for p, a, b in periods(con):
        per[p].append((a, b))
    out = []
    for r in con.execute("SELECT * FROM oc_profiles ORDER BY id"):
        d = _profile_row(r)
        d["active"] = d["id"] == act
        d["active_hours"] = _r(sum(b - a for a, b in per.get(d["id"], [])) / 3600)
        d["periods"] = [{"from": fmt_ts(a), "to": fmt_ts(b), "from_ts": a, "to_ts": b} for a, b in per.get(d["id"], [])]
        out.append(d)
    return out


def summarize_settings(p):
    """Short one-line description of a profile's settings (Polish UI / CLI)."""
    c, g = p.get("cpu") or {}, p.get("gpu") or {}
    parts = []
    co = c.get("co_per_core")
    if co:
        parts.append("CO " + (f"{co[0]:+d} all" if len(set(co)) == 1 else "/".join(f"{x:+d}" for x in co)))
    pbo = c.get("pbo") or {}
    if pbo:
        parts.append("PBO " + " ".join(f"{k}={v}" for k, v in pbo.items()))
    if "expo" in c:
        parts.append(f"EXPO {c['expo'] if not isinstance(c['expo'], bool) else ('on' if c['expo'] else 'off')}")
    if "soc_v" in c:
        parts.append(f"SoC {c['soc_v']} V")
    if "voltage_offset_mv" in g:
        parts.append(f"GPU {g['voltage_offset_mv']:+} mV")
    if g.get("power_limit_pct"):
        parts.append(f"PL {g['power_limit_pct']:+}%")
    if g.get("max_clock_offset"):
        parts.append(f"zegar {g['max_clock_offset']:+} MHz")
    if "vram_mhz" in g:
        parts.append(f"VRAM {g['vram_mhz']} MHz" + (" fast" if g.get("vram_fast_timing") else ""))
    return ", ".join(parts) or "domyślne (stock)"


# --------------------------------------------------------------------------
# per-session aggregates
# --------------------------------------------------------------------------

_DETAIL_SCALARS = ("cpu_pkg_w", "cpu_vcore", "cpu_vsoc", "tctl", "vrm_temp", "dimm_temp", "gpu_clk", "gpu_mem_clk",
                   "gpu_v", "gpu_w", "gpu_temp", "gpu_hot", "gpu_mem_temp")


def _arr(s):
    v = _json(s)
    return v if isinstance(v, list) else []


def core_loads(threads, ncores):
    """Per-core load = minimum over the core's SMT threads (a core counts as fully loaded only if all are)."""
    if not threads or not ncores:
        return []
    tpc = max(1, len(threads) // ncores)
    return [min(threads[i * tpc:(i + 1) * tpc] or [0]) for i in range(ncores)]


def _detail_stats(rows):
    """Aggregates of hw_detail rows: avg/p95/max of scalars, per-core stretching under full load."""
    st = {"detail_rows": len(rows)}
    for c in _DETAIL_SCALARS:
        vals = [r[c] for r in rows if r[c] is not None]
        if vals:
            st[c] = {"avg": _r(sum(vals) / len(vals), 3), "p95": _r(_pct(vals, 0.95), 3), "max": _r(max(vals), 3)}
    cores = defaultdict(lambda: {"loaded_n": 0, "ratio_sum": 0.0, "eff_sum": 0.0, "eff_max": 0.0})
    for r in rows:
        eff, clk = _arr(r["cpu_eff"]), _arr(r["cpu_clk"])
        loads = core_loads(_arr(r["thread_load"]), len(clk))
        for i, (e, c) in enumerate(zip(eff, clk)):
            if e:
                cores[i]["eff_max"] = max(cores[i]["eff_max"], e)
            if e and c and i < len(loads) and loads[i] >= FULL_LOAD:
                x = cores[i]
                x["loaded_n"] += 1
                x["ratio_sum"] += min(1.0, e / c)
                x["eff_sum"] += e
    st["cores"] = {str(i): {"loaded_n": x["loaded_n"], "eff_max": _r(x["eff_max"]),
                            "eff_loaded_avg": _r(x["eff_sum"] / x["loaded_n"]) if x["loaded_n"] else None,
                            "stretch_pct": _r(100 * (1 - x["ratio_sum"] / x["loaded_n"]), 2) if x["loaded_n"] else None}
                   for i, x in sorted(cores.items())}
    return st


def session_stats(con, sid):
    """UV/OC aggregates of one session (stored by the collector in oc_session_stats when it ends)."""
    s = dict(con.execute("SELECT * FROM sessions WHERE id=?", (int(sid),)).fetchone())
    end = _eff_ends(con, [s]).get(s["id"]) or time.time()
    rows = con.execute("SELECT * FROM hw_detail WHERE ts BETWEEN ? AND ? AND (session=? OR session IS NULL) ORDER BY ts",
                       (s["start_ts"], end, s["id"])).fetchall()
    st = _detail_stats(rows)
    summ = _json(s.get("summary")) or {}
    summ = summ if isinstance(summ, dict) else {}
    # application frames: frame generation would double the FPS/W of the session it was on in
    gen = summ.get("frame_gen_pct") or (summ.get("present") or {}).get("frame_gen_pct")
    fps = summ.get("app_fps_avg") or (None if gen else summ.get("fps_avg"))
    if fps is None:
        fps = con.execute(f"SELECT SUM({fs.APP_FRAMES})/SUM({fs.APP_FRAMES}/{fs.APP_FPS}) FROM frames "
                          f"WHERE session=? AND {fs.APP_FPS}>0", (s["id"],)).fetchone()[0]
    st["fps_avg"] = _r(fps)
    st["fps_1low"] = _r(summ.get("app_fps_1low") or (None if gen else summ.get("fps_1low")))
    w = (st.get("cpu_pkg_w") or {}).get("avg"), (st.get("gpu_w") or {}).get("avg")
    st["power_w"] = _r(sum(x for x in w if x), 1) if any(w) else None
    st["fps_per_w"] = _r(fps / st["power_w"], 3) if fps and st["power_w"] else None
    st["minutes"] = _r((end - s["start_ts"]) / 60)
    errs = defaultdict(int)
    for r in con.execute(f"SELECT kind FROM incidents WHERE ts BETWEEN ? AND ? AND kind IN ({','.join('?' * len(ERROR_KINDS))})",
                         (s["start_ts"], end + 120, *ERROR_KINDS)):
        errs[r[0]] += 1
    st["errors"] = dict(errs)
    st["end_reason"] = s["end_reason"]
    ctx = _json(s.get("context")) or {}
    if isinstance(ctx, dict) and ctx.get("stress_test"):
        st["test"] = ctx["stress_test"]
    return {"profile_id": s.get("profile_id"), "kind": s.get("kind") or "game", "exe": s["exe"],
            "start_ts": s["start_ts"], "end_ts": end, "stats": st}


def _stored_stats(con, since=0):
    """Stored per-session stats plus on-the-fly ones for finished sessions not aggregated yet."""
    out = {}
    for r in con.execute("SELECT * FROM oc_session_stats WHERE start_ts>=?", (since,)):
        out[r["session"]] = {"session": r["session"], "profile_id": r["profile_id"], "kind": r["kind"], "exe": r["exe"],
                             "start_ts": r["start_ts"], "end_ts": r["end_ts"], "stats": _json(r["stats"]) or {}}
    missing = [r[0] for r in con.execute(
        "SELECT id FROM sessions WHERE start_ts>=? AND end_ts IS NOT NULL ORDER BY id DESC LIMIT 300", (since,))
        if r[0] not in out]
    for sid in missing:
        x = session_stats(con, sid)
        out[sid] = {"session": sid, **x}
    # sessions.profile_id is the source of truth (it is re-tagged on back-dated activations)
    tags = {r[0]: r[1] for r in con.execute("SELECT id, profile_id FROM sessions WHERE start_ts>=?", (since,))}
    for sid, x in out.items():
        x["profile_id"] = tags.get(sid, x["profile_id"])
    return sorted(out.values(), key=lambda x: x["start_ts"])


# --------------------------------------------------------------------------
# comparisons
# --------------------------------------------------------------------------

def _wavg(items, key, sub="avg"):
    num = den = 0.0
    for st, w in items:
        v = st.get(key)
        v = v.get(sub) if isinstance(v, dict) else v
        if v is not None and w:
            num += v * w
            den += w
    return _r(num / den, 3) if den else None


def _names(con):
    return {r[0]: r[1] for r in con.execute("SELECT id, name FROM oc_profiles")}


def compare_profiles(con, exe=None, since="180d"):
    """Per profile for ONE game: FPS, 1% low, temps, power, FPS/W, errors/h. exe=None -> most played game."""
    t0 = parse_time(since)
    games = [s for s in _stored_stats(con, t0) if s["kind"] == "game" and (s["stats"].get("minutes") or 0) >= 1]
    if not exe:
        mins = defaultdict(float)
        for s in games:
            mins[s["exe"]] += s["stats"].get("minutes") or 0
        exe = max(mins, key=mins.get) if mins else None
    games = [s for s in games if exe and (s["exe"] or "").lower() == exe.lower()]
    names = _names(con)
    groups = defaultdict(list)
    for s in games:
        groups[s["profile_id"]].append(s)
    rows = []
    for pid, ss in groups.items():
        items = [(s["stats"], s["stats"].get("minutes") or 0) for s in ss]
        minutes = sum(w for _, w in items)
        errs = defaultdict(int)
        for s in ss:
            for k, v in (s["stats"].get("errors") or {}).items():
                errs[k] += v
        crashes = sum(1 for s in ss if s["stats"].get("end_reason") == "crash")
        rows.append({
            "profile_id": pid, "profile": names.get(pid, "bez profilu") if pid else "bez profilu",
            "sessions": len(ss), "minutes": _r(minutes),
            "fps_avg": _wavg(items, "fps_avg"), "fps_1low": _wavg(items, "fps_1low"),
            "cpu_temp": _wavg(items, "tctl"), "cpu_temp_p95": _wavg(items, "tctl", "p95"),
            "gpu_temp": _wavg(items, "gpu_temp"), "gpu_hotspot": _wavg(items, "gpu_hot"),
            "gpu_hotspot_p95": _wavg(items, "gpu_hot", "p95"),
            "cpu_w": _wavg(items, "cpu_pkg_w"), "gpu_w": _wavg(items, "gpu_w"), "power_w": _wavg(items, "power_w"),
            "fps_per_w": _wavg(items, "fps_per_w"), "gpu_clk": _wavg(items, "gpu_clk"), "gpu_v": _wavg(items, "gpu_v"),
            "errors": dict(errs), "game_crashes": crashes,
            "errors_per_h": _r((sum(errs.values()) + crashes) / (minutes / 60), 2) if minutes >= 6 else None,
            "small_sample": minutes < MIN_COMPARE_MIN,
            "first_ts": min(s["start_ts"] for s in ss), "last_ts": max(s["start_ts"] for s in ss),
        })
    rows.sort(key=lambda r: r["first_ts"])
    base = rows[0] if rows else None
    for r in rows:
        for k in ("fps_avg", "fps_1low", "fps_per_w", "power_w", "gpu_hotspot"):
            r[f"{k}_delta_pct"] = (_r(100 * (r[k] - base[k]) / base[k]) if base is not r and base and base.get(k)
                                   and r.get(k) is not None else None)
    return {"exe": exe, "baseline": base["profile"] if base else None, "rows": rows}


def _detail_rows(con, pid=None, since=None):
    """hw_detail rows inside the profile's activity periods (pid=None -> everything since `since`)."""
    t0 = parse_time(since) if since else 0
    if pid is None:
        return con.execute("SELECT * FROM hw_detail WHERE ts>=? ORDER BY ts", (t0,)).fetchall()
    rows = []
    for a, b in _profile_periods(con, int(pid)):
        rows += con.execute("SELECT * FROM hw_detail WHERE ts BETWEEN ? AND ? ORDER BY ts", (max(a, t0), b)).fetchall()
    return rows


def _median(vals):
    return _pct(vals, 0.5)


def vf_curve(con, pid=None, since="30d"):
    """GPU: median core voltage per 50 MHz clock bucket under load (>= 90%).
    CPU: median Vcore per 50 MHz bucket of the highest effective clock, single-thread vs all-core load."""
    rows = _detail_rows(con, pid, since)
    gpu, st, ac = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in rows:
        if r["gpu_load"] is not None and r["gpu_load"] >= 90 and r["gpu_clk"] and r["gpu_v"]:
            gpu[int(r["gpu_clk"] // 50 * 50)].append(r["gpu_v"])
        if r["cpu_vcore"]:
            eff, clk = _arr(r["cpu_eff"]), _arr(r["cpu_clk"])
            loads = core_loads(_arr(r["thread_load"]), len(clk))
            if not loads or not eff:
                continue
            busy = [i for i, x in enumerate(loads) if x >= 90]
            idle = sum(1 for x in loads if x < 30)
            if len(busy) == len(loads):
                ac[int(max(e or 0 for e in eff) // 50 * 50)].append(r["cpu_vcore"])
            elif len(busy) == 1 and idle >= len(loads) - 2 and eff[busy[0]]:
                st[int(eff[busy[0]] // 50 * 50)].append(r["cpu_vcore"])

    def pts(d, min_n=3):
        return [{"mhz": k, "v": _r(_median(v), 3), "n": len(v)} for k, v in sorted(d.items()) if len(v) >= min_n]
    return {"profile_id": pid, "gpu": pts(gpu), "cpu_single": pts(st), "cpu_all": pts(ac), "rows": len(rows)}


def per_core(con, pid=None, since="30d"):
    """Per core: WHEA 19/18 count, stretching under full load, effective clocks - which core needs a weaker CO."""
    t0 = parse_time(since)
    rows = _detail_rows(con, pid, since)
    st = _detail_stats(rows)["cores"] if rows else {}
    ranges = _profile_periods(con, int(pid)) if pid is not None else [(t0, time.time())]
    whea = defaultdict(lambda: {"whea19": 0, "whea18": 0})
    other = defaultdict(int)
    for a, b in ranges:
        for r in con.execute("SELECT kind, details FROM incidents WHERE kind IN ('whea_corrected','whea_fatal') "
                             "AND ts BETWEEN ? AND ?", (max(a, t0), b)):
            w = (_json(r["details"]) or {}).get("whea") or {}
            key = "whea19" if r["kind"] == "whea_corrected" else "whea18"
            if w.get("core") is not None and w.get("unit") in (None, "core", "l3"):
                whea[str(w["core"])][key] += 1
            else:
                other[w.get("unit") or "?"] += 1
    n = max([int(k) + 1 for k in list(st) + list(whea)] or [0])
    cores = []
    for i in range(n):
        c = {"core": i, **st.get(str(i), {}), **whea.get(str(i), {"whea19": 0, "whea18": 0})}
        c["loaded_min"] = _r((c.get("loaded_n") or 0) / 60, 1)   # 1 s rows under load (sessions)
        c["suspect"] = bool(c["whea19"] or c["whea18"] or (c.get("stretch_pct") or 0) > 3)
        cores.append(c)
    return {"profile_id": pid, "cores": cores, "whea_uncore": dict(other),
            "note": "Błędy WHEA z banków SoC/UMC (kontroler pamięci, Infinity Fabric) nie wskazują rdzenia - "
                    "podejrzewaj EXPO / VSoC / FCLK, nie Curve Optimizer." if other else None}


# --------------------------------------------------------------------------
# stability
# --------------------------------------------------------------------------

def reset_context(con, ts):
    """Load right before an unclean shutdown: low load = typical of a too aggressive Curve Optimizer at idle."""
    rows = con.execute("SELECT ts, cpu_total, gpu_load, user_idle_s FROM samples WHERE ts<? AND ts>? "
                       "ORDER BY ts DESC LIMIT 30", (ts, ts - 6 * 3600)).fetchall()
    if not rows:
        return None
    cpu = [r["cpu_total"] for r in rows if r["cpu_total"] is not None]
    gpu = [r["gpu_load"] for r in rows if r["gpu_load"] is not None]
    avg_cpu = sum(cpu) / len(cpu) if cpu else None
    return {"last_sample_ts": rows[0]["ts"], "cpu_avg": _r(avg_cpu), "gpu_avg": _r(sum(gpu) / len(gpu)) if gpu else None,
            "user_idle_s": _r(rows[0]["user_idle_s"]),
            "low_load": bool(avg_cpu is not None and avg_cpu < LOW_LOAD_CPU and (not gpu or sum(gpu) / len(gpu) < 30))}


def _test_minutes(sessions):
    """Minutes per required stress test, only for runs that ended without errors."""
    mins = defaultdict(float)
    for s in sessions:
        st = s["stats"]
        if s["kind"] not in ("stress", "manual") or sum((st.get("errors") or {}).values()) or st.get("end_reason") == "crash":
            continue
        label = (st.get("test") or s["exe"] or "").lower()
        for t in STABLE_TESTS:
            if t.lower() in label:
                mins[t] += st.get("minutes") or 0
    return mins


def stability(con, pid=None):
    """Stability criteria (plan 10.3) for a profile (default: the active one) and what is still missing."""
    pid = pid if pid is not None else active_profile_id(con)
    if pid is None:
        return {"error": "Brak aktywnego profilu - utwórz go: pm oc add \"Stock\" --activate"}
    p = profile(con, pid)
    if not p:
        return {"error": f"Nie ma profilu #{pid}"}
    ranges = _profile_periods(con, int(pid))
    errs, resets = defaultdict(int), []
    covered_s, days, idle_s = 0, set(), 0
    last_reset = 0
    for a, b in ranges:
        for r in con.execute(f"SELECT ts, kind, details FROM incidents WHERE ts BETWEEN ? AND ? AND kind IN "
                             f"({','.join('?' * len(ERROR_KINDS))}) ORDER BY ts", (a, b + 600, *ERROR_KINDS)):
            if r["kind"] == "unclean_shutdown":
                if r["ts"] - last_reset < 600:
                    continue   # Kernel-Power 41 and EventLog 6008 describe the same reset
                last_reset = r["ts"]
                resets.append({"time": fmt_ts(r["ts"]), **(reset_context(con, r["ts"]) or {})})
            errs[r["kind"]] += 1
        c = con.execute("SELECT COUNT(*), SUM(user_idle_s >= 300) FROM samples WHERE ts BETWEEN ? AND ?", (a, b)).fetchone()
        covered_s += c[0] or 0
        idle_s += c[1] or 0
        days |= {r[0] for r in con.execute(
            "SELECT date(ts,'unixepoch','localtime') AS d FROM samples WHERE ts BETWEEN ? AND ? "
            "GROUP BY d HAVING COUNT(*) >= 3600", (a, b))}
    sess = [s for s in _stored_stats(con, min((a for a, _ in ranges), default=time.time())) if s["profile_id"] == pid]
    game_min = sum(s["stats"].get("minutes") or 0 for s in sess if s["kind"] == "game")
    crashes = sum(1 for s in sess if s["kind"] == "game" and s["stats"].get("end_reason") == "crash")
    tests = _test_minutes(sess)
    hours = covered_s / 3600
    checks = [
        {"check": "0 × WHEA 19/18", "ok": not (errs["whea_corrected"] or errs["whea_fatal"] or errs["whea"]),
         "value": errs["whea_corrected"] + errs["whea_fatal"] + errs["whea"]},
        {"check": "0 × reset sterownika GPU", "ok": not errs["gpu_driver"], "value": errs["gpu_driver"]},
        {"check": "0 × Kernel-Power 41 / BSOD", "ok": not (errs["unclean_shutdown"] or errs["bsod"]),
         "value": errs["unclean_shutdown"] + errs["bsod"]},
        {"check": "0 × crash gry", "ok": not crashes, "value": crashes,
         "note": "sprawdź, czy crash nie ma innej przyczyny (pm session ID)" if crashes else None},
        {"check": f"≥ {STABLE_GAME_H} h gry", "ok": game_min >= STABLE_GAME_H * 60, "value": _r(game_min / 60)},
        {"check": f"≥ {STABLE_WORK_DAYS} dni pracy (≥ 1 h danych dziennie)", "ok": len(days) >= STABLE_WORK_DAYS,
         "value": len(days)},
        {"check": "bezczynność ≥ 2 h (biegu jałowego nie da się pominąć)", "ok": idle_s >= 7200, "value": _r(idle_s / 3600)},
    ]
    for t, need in STABLE_TESTS.items():
        hrs = need >= 60
        checks.append({"check": f"{t} ≥ {need // 60 if hrs else need} {'h' if hrs else 'min'} bez błędów",
                       "ok": tests.get(t, 0) >= need, "value": _r(tests.get(t, 0) / (60 if hrs else 1))})
    failed = [c for c in checks[:4] if not c["ok"]]
    missing = [c for c in checks[4:] if not c["ok"]]
    total_err = sum(errs.values()) + crashes
    verdict = ("niestabilny" if failed else "spełnia kryteria stabilności" if not missing else "w trakcie testów")
    return {
        "profile": {k: p[k] for k in ("id", "name", "status")}, "settings": summarize_settings(p),
        "active_since": fmt_ts(ranges[0][0]) if ranges else None, "hours_collected": _r(hours),
        "errors": dict(errs), "errors_per_h": _r(total_err / hours, 3) if hours >= 1 else None,
        "resets": resets, "low_load_resets": sum(1 for r in resets if r.get("low_load")),
        "checks": checks, "verdict": verdict, "can_mark_stable": not failed and not missing,
        "missing": [c["check"] for c in missing],
    }


def overview(con):
    """Everything the 'Podkręcanie' screen shows first: active profile + stability, profile list."""
    act = active_profile_id(con)
    od = con.execute("SELECT ts, settings FROM gpu_od_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    profs = profiles(con)
    last_act = con.execute("SELECT MAX(ts) FROM oc_profile_log").fetchone()[0] or 0
    change = con.execute("SELECT ts, details FROM incidents WHERE kind='gpu_settings_change' AND ts>? "
                         "ORDER BY ts DESC LIMIT 1", (last_act,)).fetchone()
    for p in profs:
        p["summary"] = summarize_settings(p)
    return {"active_profile_id": act, "stability": stability(con, act) if act else None, "profiles": profs,
            # Adrenalin settings changed after the last activation: the user should tag a profile
            "gpu_change": {"ts": change["ts"], "time": fmt_ts(change["ts"]),
                           "changes": (_json(change["details"]) or {}).get("changes")} if change else None,
            "gpu_od": {"ts": od["ts"], "time": fmt_ts(od["ts"]), **(_json(od["settings"]) or {})} if od else None,
            "open_stress": _rows(con, "SELECT id, exe, kind, start_ts FROM sessions WHERE end_ts IS NULL "
                                      "AND kind IN ('stress','manual','idle_soak')")}


# --------------------------------------------------------------------------
# manual stress / idle-soak sessions (written by the CLI)
# --------------------------------------------------------------------------

def stress_start(con, name, kind="manual"):
    if kind not in ("manual", "idle_soak"):
        raise ValueError("kind: manual | idle_soak")
    if con.execute("SELECT 1 FROM sessions WHERE kind IN ('manual','idle_soak') AND end_ts IS NULL").fetchone():
        raise ValueError("Trwa już ręczna sesja testu - zakończ ją: pm stress stop")
    now = time.time()
    cur = con.execute("INSERT INTO sessions(exe, start_ts, kind, profile_id, context) VALUES (?,?,?,?,?)",
                      (name, now, kind, active_profile_id(con, now), json.dumps({"stress_test": name})))
    con.commit()
    return cur.lastrowid


def stress_stop(con, reason="exit"):
    rows = con.execute("SELECT id FROM sessions WHERE kind IN ('manual','idle_soak') AND end_ts IS NULL").fetchall()
    now = time.time()
    for (sid,) in rows:
        con.execute("UPDATE sessions SET end_ts=?, end_reason=? WHERE id=?", (now, reason, sid))
    con.commit()
    return [r[0] for r in rows]


def stress_runs(con, limit=30):
    out = []
    for s in _rows(con, "SELECT id, exe, kind, start_ts, end_ts, end_reason, profile_id, context FROM sessions "
                        "WHERE kind IN ('stress','manual','idle_soak') ORDER BY id DESC LIMIT ?", (int(limit),)):
        st = (_stored_stats_for(con, s["id"]) if s["end_ts"] else None) or {}
        ctx = _json(s.pop("context")) or {}
        out.append({**s, "test": ctx.get("stress_test") if isinstance(ctx, dict) else None,
                    "time": fmt_ts(s["start_ts"]), "minutes": _r(((s["end_ts"] or time.time()) - s["start_ts"]) / 60),
                    "errors": st.get("errors"), "tctl_max": (st.get("tctl") or {}).get("max"),
                    "gpu_hot_max": (st.get("gpu_hot") or {}).get("max"), "cpu_w_avg": (st.get("cpu_pkg_w") or {}).get("avg"),
                    "gpu_w_avg": (st.get("gpu_w") or {}).get("avg"),
                    "stretch_max_pct": max([c.get("stretch_pct") or 0 for c in (st.get("cores") or {}).values()] or [0]) or None})
    return out


def _stored_stats_for(con, sid):
    r = con.execute("SELECT stats FROM oc_session_stats WHERE session=?", (sid,)).fetchone()
    if r:
        return _json(r[0]) or {}
    return session_stats(con, sid)["stats"]

