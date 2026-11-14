"""UV/OC assistant: exactly ONE next step (value, test, continue/rollback decision) from the DB state.

Read-only advisor: it never writes to the DB and never touches BIOS / Adrenalin / registry; marking a profile
stable stays the user's job. Decision order: no profile -> baseline; tuned profile failed -> rollback; blockers
(recent resets, short Stock baseline); no gain -> go back; current step unproven -> finish its test; else next step."""
import time
from datetime import datetime

from . import ocanalyze as oc
from .analyze import _json, _r, fmt_ts

LOOKBACK_D = 7
BLOCK_KINDS = ("unclean_shutdown", "bsod", "whea_fatal", "dwm_stall")
HARD_KINDS = ("unclean_shutdown", "bsod", "whea_fatal")      # also counted by oc.stability -> rollback, not a blocker
KIND_LABEL = {"unclean_shutdown": "Nieoczekiwany reset (Kernel-Power 41)", "bsod": "BSOD",
              "whea_fatal": "Fatalny błąd WHEA", "dwm_stall": "Zawieszenie pulpitu (DWM)"}
DEDUPE_S = 600                  # Kernel-Power 41 + EventLog 6008 describe the same reset

UV_LADDER = (-30, -50, -70, -80, -90, -100)
UV_SOFT_STOP = -100
PL_STEP = 10
VRAM_MHZ = 2700
CO_ALL = -10
CO_CORE_TARGET = -20
CO_CORE_STEP = 5
PBO_LIMITS = {"ppt": 160, "tdc": 105, "edc": 150}
TCTL_MAX = 90
VRM_MAX = 90
VSOC_MAX = 1.30
TREFI = 32768

GATE_GAME_MIN = 120             # intermediate gate: lighter than the full stability check
MIN_COMPARE = oc.MIN_COMPARE_MIN
GAIN = {"fps_avg": 1.0, "fps_per_w": 1.0, "temp": 1.0}   # % / degrees that count as "better"

WHERE = {
    "gpu_uv": "Adrenalin → Performance → Tuning → Manual Tuning → GPU Tuning → Voltage",
    "gpu_pl": "Adrenalin → Performance → Tuning → Manual Tuning → Power Tuning → Power Limit (%)",
    "vram": "Adrenalin → Performance → Tuning → Manual Tuning → Memory Tuning → Fast Timing: Disabled, Memory Clock",
    "co": "BIOS (Del) → Tweaker → Advanced CPU Settings → AMD Overclocking → Precision Boost Overdrive → Curve Optimizer",
    "pbo": "BIOS (Del) → Tweaker → Advanced CPU Settings → AMD Overclocking → Precision Boost Overdrive → "
           "Advanced → PBO Limits: Manual",
    "ram": "BIOS (Del) → Tweaker → Advanced Memory Settings → Channel A/B Memory Sub Timings → tREFI",
}

# gate per change kind: (what, [(test name as in oc.STABLE_TESTS, minutes, label)])
GATES = {
    "gpu": ("3DMark Steel Nomad Stress Test (pętla, min. 20 min, wynik ≥ 97%) + normalna gra",
            [("3DMark", 20, "3DMark Steel Nomad stress ≥ 20 min")]),
    "gpu_vram": ("3DMark Steel Nomad Stress Test + OCCT VRAM (≥ 20 min każdy) + normalna gra",
                 [("3DMark", 20, "3DMark Steel Nomad stress ≥ 20 min"), ("OCCT", 20, "OCCT VRAM ≥ 20 min")]),
    "cpu_co": ("CoreCycler (≈ 1 h na rdzeń, razem ≈ 8 h) + y-cruncher (≥ 30 min) + normalna gra",
               [("CoreCycler", 480, "CoreCycler ≥ 1 h na rdzeń (8 h)"), ("y-cruncher", 30, "y-cruncher ≥ 30 min")]),
    "cpu_core": ("CoreCycler na zmienionym rdzeniu (≥ 1 h) + y-cruncher (≥ 30 min) + normalna gra",
                 [("CoreCycler", 60, "CoreCycler ≥ 1 h na zmienionym rdzeniu"), ("y-cruncher", 30, "y-cruncher ≥ 30 min")]),
    "cpu_pbo": ("CoreCycler (≥ 1 h) + y-cruncher (≥ 30 min) + normalna gra, obserwuj Tctl i VRM",
                [("CoreCycler", 60, "CoreCycler ≥ 1 h"), ("y-cruncher", 30, "y-cruncher ≥ 30 min")]),
    "ram": ("OCCT Memory (≥ 60 min) + y-cruncher (≥ 30 min) + normalna gra; RAM psuje dane - rób kopię ważnych plików",
            [("OCCT", 60, "OCCT Memory ≥ 60 min"), ("y-cruncher", 30, "y-cruncher ≥ 30 min")]),
}
ROLLBACK = {
    "gpu": "Przy resecie sterownika, crashu lub WHEA: osłab offset napięcia o 10-20 mV albo wróć do poprzedniego profilu.",
    "gpu_vram": "Przy artefaktach, resecie sterownika lub crashu: wróć do poprzedniego profilu (VRAM bez OC).",
    "cpu_co": "Przy WHEA 19/18, resecie (też na biegu jałowym) lub crashu: osłab CO o 3-5 na rdzeniu z błędem albo wróć do poprzedniego profilu.",
    "cpu_core": "Przy WHEA/resecie: ten rdzeń o 3-5 słabszy; jeśli się powtarza, wróć do poprzedniego profilu.",
    "cpu_pbo": "Przy Tctl ≥ 90 °C, VRM ≥ 90 °C lub błędach: wróć do poprzedniego profilu.",
    "ram": "Przy WHEA (unit=soc), resecie lub błędach testu pamięci: wróć do EXPO bez zmian i odrzuć profil.",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _tuned(p):
    c, g = p.get("cpu") or {}, p.get("gpu") or {}
    co = c.get("co_per_core")
    return bool(any(co or []) or c.get("pbo") or "soc_v" in c or "trefi" in (p.get("notes") or "").lower()
                or any(g.get(k) for k in oc.GPU_KEYS))


def _incidents(con, now):
    """Blocking incident kinds of the last 7 days, resets deduped (KP41 + 6008)."""
    out, last_reset = [], 0
    for r in con.execute(f"SELECT ts, kind FROM incidents WHERE ts>=? AND kind IN ({','.join('?' * len(BLOCK_KINDS))}) "
                         "ORDER BY ts", (now - LOOKBACK_D * 86400, *BLOCK_KINDS)):
        if r["kind"] == "unclean_shutdown":
            if r["ts"] - last_reset < DEDUPE_S:
                out[-1] = (r["ts"], "unclean_shutdown")   # same reset: the last event counts for `until`
                continue
            last_reset = r["ts"]
        out.append((r["ts"], r["kind"]))
    return out


def _incident_blockers(evs):
    out = []
    for kind in BLOCK_KINDS:
        ts = [t for t, k in evs if k == kind]
        if ts:
            until = max(ts) + LOOKBACK_D * 86400
            out.append({"what": KIND_LABEL[kind], "detail": f"{len(ts)}× w ostatnich {LOOKBACK_D} dniach, ostatnio {fmt_ts(max(ts))}",
                        "until": fmt_ts(until), "until_ts": until})
    return out


def _previous(con, aid):
    """Profile active before the current one (rejected ones are skipped), or None."""
    seq = [p for p, _, _ in oc.periods(con)]
    i = len(seq) - 1
    while i >= 0 and seq[i] == aid:
        i -= 1
    while i >= 0:
        p = oc.profile(con, seq[i])
        if p and p["status"] != "rejected" and p["id"] != aid:
            return p
        i -= 1
    return None


def _kind(cur, prev):
    """What the last step changed: gpu | gpu_vram | cpu_co | cpu_core | cpu_pbo | ram."""
    pc, pg = (prev or {}).get("cpu") or {}, (prev or {}).get("gpu") or {}
    c, g = cur.get("cpu") or {}, cur.get("gpu") or {}
    if any(g.get(k) != pg.get(k) for k in ("vram_mhz", "vram_fast_timing")) and g.get("vram_mhz"):
        return "gpu_vram"
    if any(g.get(k) != pg.get(k) for k in oc.GPU_KEYS):
        return "gpu"
    co, pco = c.get("co_per_core") or [], pc.get("co_per_core") or []
    if co != pco:
        return "cpu_core" if sum(1 for a, b in zip(co, pco + [0] * 8) if a != b) == 1 else "cpu_co"
    if c.get("pbo") != pc.get("pbo"):
        return "cpu_pbo"
    return "ram"


def _flags(cpu, gpu):
    f = []
    co = cpu.get("co_per_core")
    if co and any(co):
        f.append(f"--co-all {co[0]}" if len(co) == 8 and len(set(co)) == 1 else "--co " + ",".join(str(x) for x in co))
    for k, flag in (("ppt", "--ppt"), ("tdc", "--tdc"), ("edc", "--edc"), ("scalar", "--scalar"), ("boost_override", "--boost")):
        if (cpu.get("pbo") or {}).get(k) is not None:
            f.append(f"{flag} {cpu['pbo'][k]}")
    if "expo" in cpu:
        e = cpu["expo"]
        f.append(f"--expo {('on' if e else 'off') if isinstance(e, bool) else chr(34) + str(e) + chr(34)}")
    if "soc_v" in cpu:
        f.append(f"--soc-v {cpu['soc_v']}")
    for k, flag in (("voltage_offset_mv", "--gpu-uv"), ("power_limit_pct", "--gpu-pl"), ("max_clock_offset", "--gpu-clk"),
                    ("vram_mhz", "--vram")):
        if gpu.get(k) not in (None, 0):
            f.append(f"{flag} {gpu[k]}")
    if "vram_fast_timing" in gpu and gpu.get("vram_mhz"):
        f.append(f"--vram-fast {'on' if gpu['vram_fast_timing'] else 'off'}")
    if gpu.get("fan_curve"):
        f.append(f"--fan \"{gpu['fan_curve']}\"")
    return " ".join(f)


def _name(cpu, gpu):
    return oc.summarize_settings({"cpu": cpu, "gpu": gpu})[:70].replace('"', "")


def _command(cpu, gpu, notes=None):
    cmd = f'pm oc add "{_name(cpu, gpu)}" {_flags(cpu, gpu)}'.replace("  ", " ").rstrip()
    if notes:
        cmd += f' --notes "{notes.replace(chr(34), "")}"'
    return cmd + " --activate"


def _apply(cpu, gpu, delta):
    return oc._merge(cpu, delta.get("cpu")), oc._merge(gpu, delta.get("gpu"))


def _weaker_uv(uv):
    return min(0, uv + (20 if uv <= -50 else 10))


def _game_bound(con):
    """True when the recent usable game sessions were mostly GPU bound."""
    kinds = []
    for r in con.execute("SELECT summary, start_ts, end_ts FROM sessions WHERE (kind IS NULL OR kind='game') "
                         "AND end_ts IS NOT NULL ORDER BY start_ts DESC LIMIT 12"):
        if (r["end_ts"] - r["start_ts"]) < 600:
            continue
        k = ((_json(r["summary"]) or {}).get("bottleneck") or {}).get("kind")
        if k:
            kinds.append(k)
    return bool(kinds) and sum(1 for k in kinds if k in ("gpu", "gpu_power")) * 2 >= len(kinds)


def _thermals(con, now):
    """Worst p95 of Tctl / VRM over recent game sessions (None = no data)."""
    t = v = None
    for s in oc._stored_stats(con, now - 30 * 86400):
        if s["kind"] != "game":
            continue
        a = (s["stats"].get("tctl") or {}).get("p95")
        b = (s["stats"].get("vrm_temp") or {}).get("p95")
        t = a if t is None or (a is not None and a > t) else t
        v = b if v is None or (b is not None and b > v) else v
    return t, v


# --------------------------------------------------------------------------
# next-step catalogue (order = the plan)
# --------------------------------------------------------------------------

def _s_uv(cpu, gpu, notes, ctx):
    uv = gpu.get("voltage_offset_mv") or 0
    nxt = next((v for v in UV_LADDER if v < uv), None)
    if nxt is None:
        return {"skip": f"Offset napięcia GPU doszedł do miękkiego limitu {UV_SOFT_STOP} mV - dalsze zyski są niepewne, "
                        "więc nie schodzimy niżej."}
    soft = nxt == UV_SOFT_STOP
    return {"kind": "gpu", "title": f"Offset napięcia GPU {nxt} mV", "where": WHERE["gpu_uv"], "value": f"{nxt} mV",
            "gpu": {"voltage_offset_mv": nxt},
            "note": f"{UV_SOFT_STOP} mV to miękki limit: dalej zyski są niepewne, a ryzyko niestabilności rośnie." if soft else None}


def _s_pl(cpu, gpu, notes, ctx):
    if (gpu.get("power_limit_pct") or 0) >= PL_STEP:
        return None
    return {"kind": "gpu", "title": f"Limit mocy GPU +{PL_STEP}%", "where": WHERE["gpu_pl"], "value": f"+{PL_STEP}%",
            "gpu": {"power_limit_pct": PL_STEP}}


def _s_vram(cpu, gpu, notes, ctx):
    if (gpu.get("vram_mhz") or 0) >= VRAM_MHZ:
        return None
    return {"kind": "gpu_vram", "title": f"VRAM {VRAM_MHZ} MHz, szybkie timingi wyłączone", "where": WHERE["vram"],
            "value": f"{VRAM_MHZ} MHz, Fast Timing off", "gpu": {"vram_mhz": VRAM_MHZ, "vram_fast_timing": False}}


def _cpu_note(ctx):
    return ("Gra jest ograniczana przez GPU, więc zysk FPS z tuningu CPU będzie mały - ten krok jest opcjonalny."
            if ctx["gpu_bound"] else None)


def _s_co_all(cpu, gpu, notes, ctx):
    if any(x < 0 for x in cpu.get("co_per_core") or []):
        return None
    return {"kind": "cpu_co", "title": f"Curve Optimizer {CO_ALL} na wszystkich rdzeniach", "where": WHERE["co"],
            "value": f"All Cores, Negative, {abs(CO_ALL)}", "cpu": {"co_per_core": [CO_ALL] * 8},
            "note": _cpu_note(ctx), "optional": ctx["gpu_bound"]}


def _s_co_core(cpu, gpu, notes, ctx):
    co = (list(cpu.get("co_per_core") or []) + [0] * 8)[:8]
    cands = [i for i in range(8) if i not in ctx["suspect"] and co[i] > CO_CORE_TARGET]
    if not cands:
        return None
    i = min(cands, key=lambda i: (-co[i], ctx["stretch"].get(i) or 0))
    co[i] = max(co[i] - CO_CORE_STEP, CO_CORE_TARGET)
    return {"kind": "cpu_core", "title": f"Curve Optimizer rdzeń {i}: {co[i]}", "where": WHERE["co"] + " → Per Core",
            "value": f"rdzeń {i} = -{abs(co[i])} (pozostałe bez zmian: {','.join(str(x) for x in co)})",
            "cpu": {"co_per_core": co}, "note": _cpu_note(ctx), "optional": ctx["gpu_bound"], "core": i}


def _s_pbo(cpu, gpu, notes, ctx):
    if (cpu.get("pbo") or {}).get("ppt") is not None:
        return None
    t, v = ctx["tctl"], ctx["vrm"]
    if t is None or v is None:
        return {"skip": "PBO pominięty: brak danych Tctl / VRM z gier - najpierw pograć z działającym kolektorem."}
    if t >= TCTL_MAX or v >= VRM_MAX:
        return {"skip": f"PBO pominięty: Tctl p95 {t:.0f} °C, VRM p95 {v:.0f} °C (wymagane < {TCTL_MAX} °C i VRM w normie)."}
    return {"kind": "cpu_pbo", "title": "Limity PBO: PPT 160 W, TDC 105 A, EDC 150 A", "where": WHERE["pbo"],
            "value": "PPT 160 W / TDC 105 A / EDC 150 A", "cpu": {"pbo": dict(PBO_LIMITS)},
            "note": _cpu_note(ctx), "optional": ctx["gpu_bound"]}


def _s_ram(cpu, gpu, notes, ctx):
    if "trefi" in (notes or "").lower():
        return None
    return {"kind": "ram", "title": f"RAM: tREFI {TREFI} (tylko ta zmiana)", "where": WHERE["ram"], "value": f"tREFI {TREFI}",
            "notes": f"tREFI {TREFI}",
            "note": f"Strojenie Kingston DDR5-5200 CL40 jest najbardziej ryzykowne dla danych (ciche błędy). "
                    f"VSoC nigdy powyżej {VSOC_MAX:.2f} V. Rób to osobno i na końcu."}


STEPS = (_s_uv, _s_pl, _s_vram, _s_co_all, _s_co_core, _s_pbo, _s_ram)


def _first_step(cpu, gpu, notes, ctx):
    skipped = []
    for fn in STEPS:
        s = fn(cpu, gpu, notes, ctx)
        if s is None:
            continue
        if "skip" in s:
            skipped.append(s["skip"])
            continue
        c, g = _apply(cpu, gpu, s)
        if any(c == rc and g == rg for rc, rg in ctx["rejected"]):
            skipped.append(f"Pominięto „{s['title']}”: taki profil został już odrzucony.")
            continue
        s["new_cpu"], s["new_gpu"] = c, g
        s["new_notes"] = "; ".join(x for x in (notes, s.get("notes")) if x) or None
        s["skipped"] = skipped
        return s
    return None


# --------------------------------------------------------------------------
# gate / criteria
# --------------------------------------------------------------------------

def _profile_sessions(con, pid):
    ranges = oc._profile_periods(con, pid)
    return [s for s in oc._stored_stats(con, min((a for a, _ in ranges), default=time.time())) if s["profile_id"] == pid]


def _gate(con, p, kind, st=None):
    """Intermediate gate for the profile's last change; returns (what, criteria)."""
    what, tests = GATES[kind]
    st = st or oc.stability(con, p["id"])
    sess = _profile_sessions(con, p["id"])
    game_min = sum(s["stats"].get("minutes") or 0 for s in sess if s["kind"] == "game")
    mins = oc._test_minutes(sess)
    errs = sum(c["value"] for c in st["checks"][:4])
    crit = [{"check": "0 błędów (WHEA, reset sterownika, reset PC, crash gry)", "ok": errs == 0, "value": errs},
            {"check": f"≥ {GATE_GAME_MIN // 60} h gry na tym profilu", "ok": game_min >= GATE_GAME_MIN, "value": _r(game_min / 60)}]
    for name, need, label in tests:
        crit.append({"check": label, "ok": mins.get(name, 0) >= need, "value": _r(mins.get(name, 0))})
    return what, crit


def _future_gate(kind):
    what, tests = GATES[kind]
    return what, ([{"check": "0 błędów (WHEA, reset sterownika, reset PC, crash gry)", "ok": False, "value": 0},
                   {"check": f"≥ {GATE_GAME_MIN // 60} h gry na tym profilu", "ok": False, "value": 0}]
                  + [{"check": label, "ok": False, "value": 0} for _, _, label in tests])


def _no_gain(con, p, prev):
    """True when the profile is measurably no better than the previous one (FPS, FPS/W, temps) and the data suffices."""
    if not prev:
        return None
    rows = {r["profile_id"]: r for r in oc.compare_profiles(con)["rows"]}
    a, b = rows.get(p["id"]), rows.get(prev["id"])
    if not a or not b or a["small_sample"] or b["small_sample"]:
        return None

    def better(k, up=True, pct=True):
        x, y = a.get(k), b.get(k)
        if x is None or y is None or not y:
            return False
        d = 100 * (x - y) / y if pct else x - y
        return (d if up else -d) > (GAIN["fps_avg"] if pct else GAIN["temp"])
    return not (better("fps_avg") or better("fps_per_w") or better("gpu_hotspot", False, False)
                or better("cpu_temp", False, False) or better("power_w", False))


# --------------------------------------------------------------------------
# result builders
# --------------------------------------------------------------------------

def _current(p, gpu_bound=None):
    g, c = p.get("gpu") or {}, p.get("cpu") or {}
    return {"profile": p["name"], "id": p["id"], "status": p["status"], "settings": oc.summarize_settings(p),
            "gpu_uv_mv": g.get("voltage_offset_mv"), "gpu_pl_pct": g.get("power_limit_pct"), "vram_mhz": g.get("vram_mhz"),
            "co_per_core": c.get("co_per_core"), "gpu_bound": gpu_bound}


def _out(phase, current=None, blockers=None, step=None, test=None, next_if_ok="", rollback_if_bad="", **extra):
    return {"phase": phase, "blocked": phase == "blocked", "blockers": blockers or [], "current": current,
            "step": step, "test": test, "next_if_ok": next_if_ok, "rollback_if_bad": rollback_if_bad, **extra}


def _tune_step(con, p, ctx, prev_text):
    cpu, gpu = p.get("cpu") or {}, p.get("gpu") or {}
    s = _first_step(cpu, gpu, p.get("notes"), ctx)
    if not s:
        st = oc.stability(con, p["id"])
        return _out("done", _current(p, ctx["gpu_bound"]),
                    step={"title": "Wszystkie kroki wykonane - pełny test stabilności", "where": "terminal",
                          "value": "pm oc stability", "command": "pm oc stability"},
                    test={"what": "Pełne kryteria stabilności (10 h gry, 3 dni, wszystkie testy)", "criteria": st["checks"]},
                    next_if_ok=f"Gdy wszystkie kryteria są spełnione, możesz sam oznaczyć profil jako stabilny: "
                               f"pm oc edit {p['id']} --status stable (PerfMonitor tego nie zrobi).",
                    rollback_if_bad="Przy błędach wróć do poprzedniego, stabilnego profilu.", can_mark_stable=st["can_mark_stable"])
    what, crit = _future_gate(s["kind"])
    cmd = _command(s["new_cpu"], s["new_gpu"], s["new_notes"] if s.get("notes") else None)
    step = {"title": s["title"], "where": s["where"], "value": s["value"], "command": cmd,
            "optional": bool(s.get("optional")), "note": s.get("note"), "skipped": s["skipped"]}
    after = _first_step(s["new_cpu"], s["new_gpu"], s["new_notes"], ctx)
    nxt = f"Jeśli test przejdzie bez błędów: {after['title']}." if after else \
        "Jeśli test przejdzie, to był ostatni krok: zrób pełne `pm oc stability`."
    return _out("tune", _current(p, ctx["gpu_bound"]), step=step,
                test={"what": f"Po wprowadzeniu zmiany: {what}", "criteria": crit},
                next_if_ok=nxt + (f" {prev_text}" if prev_text else ""), rollback_if_bad=ROLLBACK[s["kind"]])


def _rollback(con, p, prev, st, ctx):
    cpu, gpu = p.get("cpu") or {}, p.get("gpu") or {}
    kind = _kind(p, prev)
    failed = [c for c in st["checks"][:4] if not c["ok"]]
    why = ", ".join(f"{c['check'].replace('0 × ', '')}: {c['value']}" for c in failed)
    cores = oc.per_core(con, p["id"])
    whea_cores = [c for c in cores["cores"] if c["whea19"] or c["whea18"]]
    co = (list(cpu.get("co_per_core") or []) + [0] * 8)[:8]
    new_cpu, new_gpu, title, where, value = dict(cpu), dict(gpu), None, None, None
    whea = any("WHEA" in c["check"] for c in failed)
    drv = any("sterownika" in c["check"] for c in failed)
    if whea and whea_cores and any(co[c["core"]] < 0 for c in whea_cores):
        c = max((c for c in whea_cores if co[c["core"]] < 0), key=lambda c: c["whea19"] + c["whea18"])
        n = c["core"]
        co[n] = min(0, co[n] + 4)
        new_cpu["co_per_core"] = co
        title, where, value = f"Osłab Curve Optimizer na rdzeniu {n} o 4 (WHEA na rdzeniu {n})", WHERE["co"] + " → Per Core", \
            f"rdzeń {n} = {co[n]}"
    elif whea and cores["whea_uncore"]:
        new_cpu["expo"] = False
        title, where, value = ("WHEA z SoC/UMC - to nie Curve Optimizer: sprawdź EXPO i VSoC",
                               "BIOS (Del) → Tweaker → Extreme Memory Profile (X.M.P.) → Disabled (test bez EXPO)",
                               f"EXPO off na próbę; VSoC nigdy powyżej {VSOC_MAX:.2f} V")
    elif (drv or kind in ("gpu", "gpu_vram")) and gpu.get("voltage_offset_mv"):
        uv = _weaker_uv(gpu["voltage_offset_mv"])
        new_gpu["voltage_offset_mv"] = uv
        title, where, value = f"Osłab offset napięcia GPU do {uv} mV", WHERE["gpu_uv"], f"{uv} mV"
    elif kind == "cpu_co" and any(co):
        new_cpu["co_per_core"] = [min(0, x + 5) for x in co]
        title, where, value = "Osłab Curve Optimizer o 5 na wszystkich rdzeniach", WHERE["co"], \
            f"All Cores = {new_cpu['co_per_core'][0]}"
    elif kind == "cpu_core" and prev is not None:
        pco = (list(prev["cpu"].get("co_per_core") or []) + [0] * 8)[:8]
        i = next(i for i in range(8) if co[i] != pco[i])
        co[i] = min(0, co[i] + 5)
        new_cpu["co_per_core"] = co
        title, where, value = f"Osłab Curve Optimizer na rdzeniu {i} o 5", WHERE["co"] + " → Per Core", f"rdzeń {i} = {co[i]}"
    if title:
        cmd = _command(new_cpu, new_gpu, p.get("notes"))
    elif prev:
        title, where, value = f"Wróć do poprzedniego profilu: {prev['name']}", "Ustawienia z profilu " + prev["name"], \
            oc.summarize_settings(prev)
        cmd = f"pm oc activate {prev['id']}"
    else:
        title, where, value, cmd = "Wróć do ustawień domyślnych", "BIOS / Adrenalin → Reset to defaults", "domyślne", None
    what, crit = _future_gate(kind)
    step = {"title": title, "where": where, "value": value, "command": cmd,
            "reject_command": f"pm oc edit {p['id']} --status rejected", "failed": why}
    return _out("rollback", _current(p, ctx["gpu_bound"]), step=step,
                test={"what": f"Po zmianie powtórz: {what}", "criteria": crit},
                next_if_ok=f"Zostań przy poprawionym profilu i dokończ testy; oznacz stary jako odrzucony "
                           f"(pm oc edit {p['id']} --status rejected).",
                rollback_if_bad=(f"Jeśli błędy wrócą: pm oc activate {prev['id']} i zostań przy profilu „{prev['name']}”."
                                 if prev else "Jeśli błędy wrócą: wróć do ustawień domyślnych."))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def next_step(con, now=None):
    """The single next UV/OC step. Pure read; see the module docstring for the decision order."""
    now = time.time() if now is None else now
    aid = oc.active_profile_id(con)
    ctx = {"gpu_bound": _game_bound(con), "suspect": set(), "stretch": {}, "tctl": None, "vrm": None,
           "rejected": [(r["cpu"], r["gpu"]) for r in map(oc._profile_row, con.execute(
               "SELECT * FROM oc_profiles WHERE status='rejected'"))]}
    if aid is None:
        # from now, not back-dated: older resets would count against the baseline for good
        cmd = "pm oc add Stock --expo on --activate"
        return _out("baseline", None, step={
            "title": "Utwórz profil bazowy Stock", "where": "terminal (nic nie zmieniasz w BIOS ani Adrenalin)",
            "value": "obecne ustawienia, EXPO włączone", "command": cmd},
            test={"what": "Pograj na ustawieniach domyślnych, nic nie zmieniaj", "criteria": [
                {"check": f"≥ {oc.STABLE_GAME_H} h gry na Stock", "ok": False, "value": 0},
                {"check": f"≥ {oc.STABLE_WORK_DAYS} dni danych", "ok": False, "value": 0}]},
            next_if_ok="Po zebraniu bazy (i bez resetów) pierwszy krok to offset napięcia GPU -30 mV.",
            rollback_if_bad="Nie dotyczy - nic nie zmieniasz.", warnings=_incident_blockers(_incidents(con, now)))
    p = oc.profile(con, aid)
    st = oc.stability(con, aid)
    prev = _previous(con, aid)
    tuned = _tuned(p)
    cur = _current(p, ctx["gpu_bound"])
    since = (oc._profile_periods(con, aid) or [(now, now)])[0][0]
    failed = [c for c in st["checks"][:4] if not c["ok"]]

    if tuned and failed:
        pc = oc.per_core(con, aid)
        ctx["suspect"] = {c["core"] for c in pc["cores"] if c["suspect"]}
        return _rollback(con, p, prev, st, ctx)

    # blockers
    blockers, evs = [], _incidents(con, now)
    if tuned:   # hard errors after tuning are the profile's fault -> rollback path
        evs = [(t, k) for t, k in evs if not (k in HARD_KINDS and t >= since)]
    blockers += _incident_blockers(evs)
    if not tuned:
        pre = [c for c in failed if not c["check"].endswith("crash gry")]
        if pre:
            blockers.append({"what": "Niestabilność przed strojeniem",
                             "detail": "Na profilu bez strojenia są błędy (" + ", ".join(f"{c['check'].replace('0 × ', '')}: {c['value']}" for c in pre)
                                       + "). Overclocking daje identyczne objawy - najpierw znajdź przyczynę (pm health, pm incidents); "
                                         "po naprawie załóż nowy profil: pm oc add \"Stock po naprawie\" --expo on --activate"})
        game = next((c for c in st["checks"] if c["check"].startswith("≥ 10 h gry")), {})
        days = next((c for c in st["checks"] if c["check"].startswith("≥ 3 dni")), {})
        if not (game.get("ok") and days.get("ok")):
            blockers.append({"what": "Za mało danych bazowych na Stock",
                             "detail": f"gra {game.get('value', 0)} / {oc.STABLE_GAME_H} h, dni pracy {days.get('value', 0)} / {oc.STABLE_WORK_DAYS}"})
    if blockers:
        dates = [b["until_ts"] for b in blockers if b.get("until_ts")]
        prog = [{"check": f"≥ {oc.STABLE_GAME_H} h gry na Stock", **{k: next((c[k] for c in st["checks"] if c["check"].startswith("≥ 10 h gry")), None) for k in ("ok", "value")}},
                {"check": f"≥ {oc.STABLE_WORK_DAYS} dni danych", **{k: next((c[k] for c in st["checks"] if c["check"].startswith("≥ 3 dni")), None) for k in ("ok", "value")}}]
        return _out("blocked", cur, blockers=blockers,
                    test={"what": "Nie zmieniaj niczego - zbieraj dane na obecnych ustawieniach", "criteria": prog},
                    next_if_ok=("Po " + fmt_ts(max(dates))[:10] + " " if dates else "Po zebraniu bazy ") +
                               f"pierwszy krok: offset napięcia GPU {UV_LADDER[0]} mV.",
                    rollback_if_bad="", until=fmt_ts(max(dates)) if dates else None, until_ts=max(dates) if dates else None)

    pc = oc.per_core(con, aid)
    ctx["suspect"] = {c["core"] for c in pc["cores"] if c["suspect"]}
    ctx["stretch"] = {c["core"]: c.get("stretch_pct") for c in pc["cores"]}
    ctx["tctl"], ctx["vrm"] = _thermals(con, now)

    prev_text = ""
    if tuned:
        gain = _no_gain(con, p, prev)
        if gain:
            return _out("no_gain", cur, step={
                "title": f"Wróć do poprzedniego profilu: {prev['name']}", "where": "Ustawienia z profilu " + prev["name"],
                "value": oc.summarize_settings(prev), "command": f"pm oc activate {prev['id']}",
                "reject_command": f"pm oc edit {p['id']} --status rejected",
                "note": "Ten krok nic nie dał (FPS, FPS/W i temperatury bez poprawy) - zostań przy poprzednim."},
                test={"what": "Porównanie profili (pm oc compare)", "criteria": [{"check": "FPS, FPS/W lub temperatury lepsze niż poprzedni profil", "ok": False, "value": None}]},
                next_if_ok="Po powrocie przejdź do kolejnej kategorii strojenia (ten wariant jest pomijany).",
                rollback_if_bad="")
        if gain is False:
            prev_text = "Ostatni krok dał poprawę względem poprzedniego profilu."
        kind = _kind(p, prev)
        what, crit = _gate(con, p, kind, st)
        if not all(c["ok"] for c in crit):
            todo = [c for c in crit if not c["ok"]]
            tname = next(((t, c) for c in todo for t in GATES[kind][1] if t[2] == c["check"]), None)
            miss = tname[1] if tname else todo[0]
            tname = tname[0] if tname else None
            cmd = f'pm stress start "{tname[0]}"' if tname else "gra jak zwykle"
            return _out("verify", cur, step={
                "title": "Dokończ testy obecnego kroku: " + miss["check"], "where": "po teście: pm stress stop",
                "value": oc.summarize_settings(p), "command": cmd}, test={"what": what, "criteria": crit},
                next_if_ok="Gdy wszystkie kryteria są spełnione: kolejny krok strojenia.", rollback_if_bad=ROLLBACK[kind])
    return _tune_step(con, p, ctx, prev_text)
