"""Hard-reset forensics: the last minute before the last sample of every unclean shutdown.

PSU rails (+12V/+5V/+3.3V/+3V standby) come from the board's Super I/O (ITE IT8689E here) through resistor dividers:
read once per second (in game/stress sessions, every 10 s otherwise), with a resolution of tens of mV on the 12 V rail
and a factory calibration of a few %. That can show a sustained sag or a rail outside the ATX tolerance (+-5 %) in the
seconds before the reset. It cannot see a millisecond transient (what trips the PSU's OCP/UVP or a GPU power spike),
so a clean reading does not prove the PSU is fine. Human-readable strings are Polish."""
import json
import logging
import statistics
import time

from . import analyze as an
from . import config

log = logging.getLogger("perfmon.resetforensics")

WINDOW_S = 60
BASELINE_S = 1800          # baseline when the reset did not happen in a session: the half hour before the window
ATX_TOL = 0.05
SAG_PCT = 3.0              # rail minimum this far below its own baseline = sag worth reporting
HEAVY_GPU_W = 200          # RX 9070 XT gaming draw is ~250-300 W
LAST_S = 10                # "at the moment of the reset" = the last seconds of data
FREEZE_MS = 2000           # DWM / shell unresponsive this long while samples go on = graphics hung, kernel alive
ACTIVE_S = 60              # ...while the user was at the PC (with the display asleep DwmFlush stalls on its own)
QUICK_BOOT_S = 120         # back up within this = restart by itself (a tripped PSU stays off until the button)
BOOT_TO_EVENT_S = 300      # Kernel-Power 41 / 6008 are logged ~10 s after boot
ALIVE_AFTER_S = 120        # an event logged this long after the last sample: Windows ran on without the collector
FREEZE_END_S = 3           # the freeze must last to the end of data (dwm_ms dips below FREEZE_MS for a second at times)
RAILS = {"v12": ("+12V", 12.0), "v5": ("+5V", 5.0), "v33": ("+3.3V", 3.3), "v3sb": ("+3V Standby", 3.3)}
TEMPS = {"tctl": ("CPU Tctl", 95), "gpu_hot": ("GPU hotspot", 105), "gpu_mem_temp": ("GPU VRAM", 100),
         "vrm_temp": ("VRM CPU", 100), "pch_temp": ("chipset (PCH)", 90), "pcie_temp": ("PCIe x16", 80)}
LIMITS_TXT = ("Super I/O mierzy szyny raz na sekundę z rozdzielczością kilkudziesięciu mV i fabryczną kalibracją rzędu "
              "kilku %: widać trwały spadek napięcia lub szynę poza tolerancją ATX (+-5%), ale nie milisekundowy zapad, "
              "który wyzwala zabezpieczenie zasilacza (OCP/UVP) - czysty odczyt nie dowodzi, że zasilacz jest sprawny.")


def _vals(rows, k):
    return [r[k] for r in rows if r.get(k) is not None]


def _mx(rows, k):
    v = _vals(rows, k)
    return an._r(max(v)) if v else None


def _avg(rows, k):
    v = _vals(rows, k)
    return an._r(sum(v) / len(v)) if v else None


def _boot(con, reset_ts):
    """(last sample before the boot, boot ts) from the collector's reboot incident of the boot that logged the event."""
    for (d,) in con.execute("SELECT details FROM incidents WHERE kind='reboot' AND ts<? AND ts>? ORDER BY ts DESC LIMIT 3",
                            (reset_ts, reset_ts - 30 * 86400)):
        try:
            d = json.loads(d or "{}")
        except ValueError:
            continue
        if d.get("boot") and d.get("last_sample") and 0 <= reset_ts - d["boot"] <= BOOT_TO_EVENT_S:
            return d["last_sample"], d["boot"]
    return None, None


_CACHE = {}   # (db path, incident id) -> forensics of resets older than an hour (their data no longer changes)


def fill(con, unclean):
    """Forensics for _unclean_shutdowns entries the collector did not store; never breaks the health report."""
    for u in unclean:
        if u.get("forensics"):
            continue
        key = (str(config.DB_PATH), u["id"])
        if key not in _CACHE:
            try:
                f = build(con, u["ts"])
            except Exception:
                log.exception("reset forensics for incident %s", u["id"])
                f = None
            if u["ts"] > time.time() - 3600:
                u["forensics"] = f
                continue
            _CACHE[key] = f
        u["forensics"] = _CACHE[key]


def build(con, reset_ts):
    """Forensics dict for an unclean shutdown logged at reset_ts (the event comes at the next boot), or None when
    the collector has no data from right before it."""
    t_last, boot = _boot(con, reset_ts)
    if t_last is None:   # no reboot incident (collector started later): trust only a recent last sample
        t_last = (an._one(con, "SELECT MAX(ts) AS t FROM samples WHERE ts<? AND ts>?",
                          (reset_ts, reset_ts - 6 * 3600)) or {}).get("t")
    last = an._one(con, "SELECT ts, game_session FROM samples WHERE ts<=? ORDER BY ts DESC LIMIT 1",
                   (t_last,)) if t_last else None
    if not last:
        return None
    t1 = last["ts"]
    t0 = t1 - WINDOW_S
    sid = last["game_session"]
    out = {"last_sample_ts": t1, "last_sample": an.fmt_ts(t1), "window_s": WINDOW_S, "session": sid,
           "down_s": round(boot - t1) if boot else None}   # last data -> next boot: off, or restarted by itself
    # the collector stopped (or the PC slept) before the reset: the last minute of data is not the reset moment
    alive = con.execute("SELECT MAX(ts) FROM events WHERE ts>? AND ts<?",
                        (t1 + ALIVE_AFTER_S, (boot or reset_ts - BOOT_TO_EVENT_S) - 5)).fetchone()[0]
    if alive:
        out.update(alive_until=an.fmt_ts(alive), down_s=None)
    if sid is not None:
        s = an._one(con, "SELECT exe, kind, start_ts FROM sessions WHERE id=?", (sid,))
        if s:
            out.update(exe=s["exe"], session_kind=s["kind"] or "game")
    smp = an._rows(con, "SELECT ts, cpu_total, gpu_load, cpu_power_w, gpu_power_w, cpu_temp, gpu_hotspot, gpu_mem_temp, "
                        "vrm_temp, ram_pct, fg_exe, ui_ms, dwm_ms, user_idle_s FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
                   (t0, t1))
    tail = [s for s in smp if s["ts"] >= t1 - LAST_S]
    out["frozen_s_before"] = _frozen_tail(smp, t1)
    out["load"] = {"cpu_avg_pct": _avg(smp, "cpu_total"), "gpu_avg_pct": _avg(smp, "gpu_load"),
                   "gpu_last_pct": an._r(smp[-1]["gpu_load"]) if smp and smp[-1]["gpu_load"] is not None else None,
                   "ram_max_pct": _mx(smp, "ram_pct"), "fg_exe": smp[-1]["fg_exe"] if smp else None}
    out["power"] = {"cpu_max_w": _mx(smp, "cpu_power_w"), "gpu_max_w": _mx(smp, "gpu_power_w"),
                    "gpu_last_w": an._r(smp[-1]["gpu_power_w"]) if smp and smp[-1]["gpu_power_w"] is not None else None,
                    "gpu_last10_max_w": _mx(tail, "gpu_power_w"), "cpu_last10_max_w": _mx(tail, "cpu_power_w")}
    temps = {"tctl": _mx(smp, "cpu_temp"), "gpu_hot": _mx(smp, "gpu_hotspot"), "gpu_mem_temp": _mx(smp, "gpu_mem_temp"),
             "vrm_temp": _mx(smp, "vrm_temp")}
    det = an._rows(con, "SELECT ts, v12, v5, v33, v3sb, pch_temp, pcie_temp, sys_temp FROM hw_detail "
                        "WHERE ts BETWEEN ? AND ? ORDER BY ts", (t0, t1))
    out["detail_rows"] = len(det)
    for k in ("pch_temp", "pcie_temp", "sys_temp"):
        temps[k] = _mx(det, k)
    out["temps"] = {k: v for k, v in temps.items() if v is not None}
    out["rails"] = _rails(con, det, sid, t0)
    out["notes"] = notes(out)
    return out


def _frozen_tail(smp, t1):
    """Seconds the display had been hung when the data stopped: a DWM/shell freeze still going on in the last
    LAST_S seconds, measured back to its start (short stalls earlier in the minute are common and mean nothing).
    Only while the user was active: with the display asleep DwmFlush stalls without anything being wrong."""
    start, active = None, False
    for s in reversed(smp):
        if (s["dwm_ms"] or 0) >= FREEZE_MS or (s["ui_ms"] or 0) >= FREEZE_MS:
            if start is None and s["ts"] < t1 - FREEZE_END_S:
                return None   # recovered before the data stopped
            start = s["ts"]
            active = active or (s["user_idle_s"] is not None and s["user_idle_s"] < ACTIVE_S)
        elif start is not None and start - s["ts"] > 3:
            break
    return round(t1 - start) if start is not None and active else None


def _rails(con, det, sid, t0):
    """Per rail: minimum and last reading of the window vs the median of the session before it (or the half hour
    before it outside sessions)."""
    if not det:
        return {}
    if sid is not None:
        base = an._rows(con, "SELECT v12, v5, v33, v3sb FROM hw_detail WHERE session=? AND ts<?", (sid, t0))
    else:
        base = an._rows(con, "SELECT v12, v5, v33, v3sb FROM hw_detail WHERE ts BETWEEN ? AND ?", (t0 - BASELINE_S, t0))
    rails = {}
    for k, (label, nominal) in RAILS.items():
        v = _vals(det, k)
        if not v:
            continue
        b = _vals(base, k)
        ref = statistics.median(b) if len(b) >= 10 else None
        lo = min(v)
        out_atx = lambda x: abs(x - nominal) > ATX_TOL * nominal
        sag_pct = round(100 * (ref - lo) / ref, 1) if ref else None
        rails[k] = {"label": label, "min": round(lo, 3), "last": round(v[-1], 3),
                    "baseline": round(ref, 3) if ref else None, "sag_pct": sag_pct, "outside_atx": out_atx(lo),
                    # a drop vs its own baseline; a rail that always reads off-spec is a calibration offset, not a sag
                    "sag": bool((sag_pct or 0) >= SAG_PCT or (out_atx(lo) and ref and not out_atx(ref)))}
    return rails


def notes(f):
    """Polish interpretation, honest about what the per-second rail readings can and cannot show."""
    n = []
    if f.get("alive_until"):
        return [f"Po ostatniej próbce ({f['last_sample']}) Windows zapisywał jeszcze zdarzenia do {f['alive_until']}: "
                "system działał dalej bez kolektora (albo był uśpiony), więc ostatnia minuta danych nie opisuje chwili "
                "resetu."]
    rails = f.get("rails") or {}
    sag = [r for r in rails.values() if r.get("sag")]
    for r in sag:
        base = f", norma przed resetem {r['baseline']:.2f} V (-{r['sag_pct']}%)" if r.get("baseline") else ""
        n.append(f"{r['label']} spadło do {r['min']:.2f} V w ostatniej minucie{base}"
                 + (" - poza tolerancją ATX" if r["outside_atx"] else "") + ".")
    for r in rails.values():
        if r["outside_atx"] and not r["sag"]:
            n.append(f"{r['label']} odczytuje stale ok. {r['min']:.2f} V, także wcześniej - to raczej przesunięcie "
                     "kalibracji czujnika płyty niż spadek napięcia; prawdziwą wartość pokaże tylko multimetr.")
    if rails and not sag:
        n.append("Szyny zasilacza w ostatniej minucie bez trwałego spadku (" +
                 ", ".join(f"{r['label']} min {r['min']:.2f} V" for r in rails.values()) + ").")
    elif not rails:
        n.append("Brak odczytów szyn zasilacza z tej chwili (zbierane od tej wersji, co 1 s w grze, co 10 s poza nią).")
    hot = [f"{TEMPS[k][0]} {v}°C" for k, v in (f.get("temps") or {}).items() if k in TEMPS and v >= TEMPS[k][1]]
    if hot:
        n.append("Przegrzanie tuż przed resetem: " + ", ".join(hot) + ".")
    if f.get("frozen_s_before") is not None:
        fz = f["frozen_s_before"]
        n.append(f"Obraz (DWM/pulpit) przestał odpowiadać {'co najmniej ' if fz >= WINDOW_S - 1 else ''}{fz} s przed "
                 "końcem danych, przy aktywnym użytkowniku, a kolektor dalej "
                 "zapisywał próbki: jądro działało, zawiesiła się grafika (sterownik GPU / niestabilny UV/OC GPU). "
                 "Reset przyszedł później (ręczny albo watchdog) - to nie nagła utrata zasilania.")
    elif heavy(f):
        n.append(f"Reset pod dużym obciążeniem GPU (ostatnie {LAST_S} s do {f['power']['gpu_last10_max_w'] or 0:.0f} W), "
                 "dane urywają się bez wcześniejszego zawieszenia - pasuje do zabezpieczenia zasilacza przy skoku poboru "
                 "karty albo niestabilnego UV/OC GPU.")
    elif idle(f):
        n.append("Reset przy niskim obciążeniu - zasilacz rzadko jest winny; częściej niestabilny Curve Optimizer na "
                 "biegu jałowym, RAM/EXPO lub sterownik.")
    down = f.get("down_s")
    if down is not None and down <= QUICK_BOOT_S:
        n.append(f"Komputer wstał sam po {down} s: to restart, nie wyłączenie. Zadziałanie zabezpieczenia zasilacza "
                 "zwykle zostawia komputer wyłączonym do wciśnięcia przycisku, więc szybki restart bez blue screena "
                 "częściej oznacza reset sprzętowy (niestabilny CPU/RAM: UV/OC, EXPO) albo przycisk reset.")
    elif down is not None:
        n.append(f"Komputer był wyłączony {_dur(down)} do następnego startu: wyłączył się całkowicie (zabezpieczenie "
                 "zasilacza, przegrzanie lub przytrzymany przycisk) albo zawiesił się i został wyłączony ręcznie.")
    if rails:
        n.append(LIMITS_TXT)
    return n


def _dur(s):
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min" if s >= 60 else f"{s} s"


def heavy(f):
    """GPU drawing a lot in the last seconds (not just once in the minute) or fully loaded over the minute."""
    p, ld = f.get("power") or {}, f.get("load") or {}
    return bool((p.get("gpu_last10_max_w") or 0) >= HEAVY_GPU_W or (ld.get("gpu_avg_pct") or 0) >= 80)


def idle(f):
    ld = f.get("load") or {}
    return ld.get("gpu_avg_pct") is not None and ld["gpu_avg_pct"] < 30 and (ld.get("cpu_avg_pct") or 100) < 30


def findings(unclean):
    """`pm health` findings across the resets that have forensics (rep["unclean_shutdowns"])."""
    # alive_until: the data stopped well before those resets, nothing to judge
    fs = [u["forensics"] for u in unclean or [] if u.get("forensics") and not u["forensics"].get("alive_until")]
    if not fs:
        return []
    frozen = [f for f in fs if f.get("frozen_s_before") is not None]
    n_heavy = sum(1 for f in fs if heavy(f) and f.get("frozen_s_before") is None)
    quick = sum(1 for f in fs if f.get("down_s") is not None and f["down_s"] <= QUICK_BOOT_S)
    with_rails = [f for f in fs if f.get("rails")]
    sag = [f for f in with_rails if any(r.get("sag") for r in f["rails"].values())]
    hot = [f for f in fs if any(k in TEMPS and v >= TEMPS[k][1] for k, v in (f.get("temps") or {}).items())]
    games = sum(1 for f in fs if f.get("session") is not None)
    parts = [f"{len(fs)} resetów z danymi z ostatniej minuty: {games} w trakcie gry/testu, {len(frozen)} po zawieszeniu "
             f"obrazu, {n_heavy} nagle pod dużym obciążeniem GPU, {quick} z samoczynnym restartem, {len(hot)} z przegrzaniem."]
    if with_rails:
        parts.append(f"Odczyt szyn zasilacza: {len(with_rails)} resetów, w {len(sag)} trwały spadek napięcia przed resetem.")
        parts.append(LIMITS_TXT)
    else:
        parts.append("Szyny zasilacza (+12V/+5V/+3.3V) nie były jeszcze zapisywane w chwili tych resetów - "
                     "kolejny reset pokaże ich ostatnią minutę.")
    if sag:
        sev, title = "serious", "Spadek napięcia zasilacza przed resetem"
    elif len(frozen) >= 2 and len(frozen) >= 0.5 * len(fs):
        sev, title = "serious", "Resety po zawieszeniu grafiki"
        parts.append("Najpierw zawieszał się obraz, system żył dalej: szukaj w sterowniku GPU i UV/OC karty (cofnij na "
                     "test, czysta instalacja Adrenalin przez DDU), nie w zasilaczu.")
    elif n_heavy >= 2 and n_heavy >= 0.5 * len(fs):
        sev, title = "serious", "Resety nagle pod obciążeniem GPU"
        parts.append("Wzorzec pasuje do zasilacza wyłączającego się przy skokach poboru karty albo niestabilnego UV/OC "
                     "GPU: na test cofnij UV/OC GPU i obniż limit mocy (Adrenalin -10%); jeśli resety znikną, sprawdź "
                     "zasilacz (moc, osobne przewody 8-pin/12V-2x6 do karty).")
    else:
        sev, title = "info", "Ostatnia minuta przed resetami"
    return [{"severity": sev, "title": title, "detail": " ".join(parts)}]
