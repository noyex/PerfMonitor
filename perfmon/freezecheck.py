"""Live freeze diagnosis (`pm freeze-check`): run while the desktop is frozen, e.g. from a remote agent session.

Measures right now, independently of the collector: DWM composition (DwmFlush), explorer responsiveness,
foreground window, GPU 3D engine load per adapter, display modes (refresh rates, which GPU drives which
monitor), top CPU processes, memory, plus the collector's latest sample and recent incidents/events.
"""
import ctypes
import threading
import time
from ctypes import wintypes

import psutil
import win32api
import win32con
import win32pdh

from . import analyze as an
from .watchdog import SMTO_ABORTIFHUNG, SMTO_BLOCK, WM_NULL, foreground, user32

DWM_TIMEOUT_S = 20
SHELL_TIMEOUT_MS = 5000


def dwm_flush_ms(timeout_s=DWM_TIMEOUT_S):
    """Time one DwmFlush (returns when DWM has composed a frame). None = still blocked after timeout."""
    res = {}

    def run():
        t0 = time.perf_counter()
        ctypes.WinDLL("dwmapi").DwmFlush()
        res["ms"] = (time.perf_counter() - t0) * 1000

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s)
    return res.get("ms")


def shell_ping_ms():
    hwnd = user32.GetShellWindow()
    if not hwnd:
        return None, "brak okna powłoki (explorer nie działa?)"
    out = ctypes.c_size_t()
    t0 = time.perf_counter()
    ok = user32.SendMessageTimeoutW(hwnd, WM_NULL, 0, 0, SMTO_BLOCK | SMTO_ABORTIFHUNG, SHELL_TIMEOUT_MS,
                                    ctypes.byref(out))
    ms = (time.perf_counter() - t0) * 1000
    return (ms if ok else max(ms, SHELL_TIMEOUT_MS)), (None if ok else "timeout / okno uznane za zawieszone")


def gpu_3d_load(seconds=1.0):
    """Sum of 3D engine utilization per adapter LUID (PDH GPU Engine counters, no admin needed)."""
    q = win32pdh.OpenQuery()
    try:
        c = win32pdh.AddEnglishCounter(q, r"\GPU Engine(*engtype_3D)\Utilization Percentage")
        win32pdh.CollectQueryData(q)
        time.sleep(seconds)
        win32pdh.CollectQueryData(q)
        arr = win32pdh.GetFormattedCounterArray(c, win32pdh.PDH_FMT_DOUBLE | win32pdh.PDH_FMT_NOCAP100)
    except Exception as e:
        return {"error": str(e)}
    finally:
        win32pdh.CloseQuery(q)
    per = {}
    for inst, v in arr.items():
        parts = inst.split("_")
        luid = "_".join(parts[parts.index("luid") + 1:parts.index("luid") + 3]) if "luid" in parts else inst
        per[luid] = per.get(luid, 0) + v
    return {k: round(min(v, 100), 1) for k, v in per.items()}


def displays():
    out = []
    i = 0
    while True:
        try:
            d = win32api.EnumDisplayDevices(None, i)
        except Exception:
            break
        i += 1
        if not d.StateFlags & win32con.DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue
        try:
            m = win32api.EnumDisplaySettings(d.DeviceName, win32con.ENUM_CURRENT_SETTINGS)
            mode = f"{m.PelsWidth}x{m.PelsHeight}@{m.DisplayFrequency} Hz"
        except Exception:
            mode = "?"
        out.append({"display": d.DeviceName, "adapter": d.DeviceString, "mode": mode,
                    "primary": bool(d.StateFlags & win32con.DISPLAY_DEVICE_PRIMARY_DEVICE)})
    return out


def top_procs(seconds=1.0, n=10):
    procs = list(psutil.process_iter(["pid", "name", "memory_info"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(seconds)
    rows = []
    ncpu = psutil.cpu_count()
    for p in procs:
        try:
            if p.pid == 0:
                continue
            rows.append({"name": p.info["name"], "pid": p.pid, "cpu_pct": round(p.cpu_percent(None) / ncpu, 1),
                         "mem_mb": round(p.info["memory_info"].rss / 2**20) if p.info["memory_info"] else None})
        except Exception:
            pass
    rows.sort(key=lambda r: -r["cpu_pct"])
    return rows[:n]


def process_info(name):
    out = []
    for p in psutil.process_iter(["pid", "name", "create_time"]):
        if (p.info["name"] or "").lower() == name:
            out.append({"pid": p.pid, "started": an.fmt_ts(p.info["create_time"]),
                        "age_s": round(time.time() - p.info["create_time"])})
    return out


def run(con=None):
    t_start = time.time()
    r = {"at": an.fmt_ts(t_start)}
    # DWM first: it is the measurement that can block for the full timeout, so run it alongside the rest
    dwm = {}
    th = threading.Thread(target=lambda: dwm.update(ms=dwm_flush_ms()), daemon=True)
    th.start()
    r["shell_ping_ms"], r["shell_note"] = shell_ping_ms()
    hwnd, pid, hung = foreground()
    try:
        fg = psutil.Process(pid).name() if pid else None
    except Exception:
        fg = "?"
    r["foreground"] = {"exe": fg, "pid": pid, "hung": hung}
    r["gpu_3d_by_adapter"] = gpu_3d_load()
    r["displays"] = displays()
    r["top_procs"] = top_procs()
    vm = psutil.virtual_memory()
    r["cpu_pct"] = psutil.cpu_percent(0.5)
    r["ram_pct"] = vm.percent
    r["dwm_process"] = process_info("dwm.exe")
    r["explorer_process"] = process_info("explorer.exe")
    th.join(DWM_TIMEOUT_S + 1)
    r["dwm_flush_ms"] = round(dwm["ms"], 1) if dwm.get("ms") is not None else None
    r["dwm_blocked"] = dwm.get("ms") is None
    if con is not None:
        lt = an.latest(con)
        s = lt.get("sample") or {}
        r["collector"] = {"alive": lt.get("collector_alive"), "sample_age_s": lt.get("sample_age_s"),
                          "ui_ms": an._r(s.get("ui_ms")), "dwm_ms": an._r(s.get("dwm_ms")), "gpu_load": s.get("gpu_load"),
                          "gpu_clock_mhz": s.get("gpu_clock_mhz"), "fg_exe": s.get("fg_exe")}
        r["recent_incidents"] = [{"time": i["time"], "kind": i["kind"], "title": i["title"]}
                                 for i in an.incidents(con, t_start - 600)]
        r["recent_events"] = an._rows(con, "SELECT ts, provider, event_id, level, substr(message,1,200) AS message "
                                           "FROM events WHERE ts > ? AND level <= 3 ORDER BY ts DESC LIMIT 15",
                                      (t_start - 600,))
        for e in r["recent_events"]:
            e["time"] = an.fmt_ts(e.pop("ts"))
    r["verdict"] = verdict(r)
    r["took_s"] = round(time.time() - t_start, 1)
    return r


def verdict(r):
    v = []
    dwm_bad = r["dwm_blocked"] or (r["dwm_flush_ms"] or 0) >= 2000
    shell_bad = (r["shell_ping_ms"] or 0) >= 2000
    gpu = r["gpu_3d_by_adapter"] if isinstance(r["gpu_3d_by_adapter"], dict) and "error" not in r["gpu_3d_by_adapter"] else {}
    gpu_idle = bool(gpu) and max(gpu.values()) < 2
    if dwm_bad:
        v.append(f"DWM nie składa obrazu (DwmFlush {'> %d s' % DWM_TIMEOUT_S if r['dwm_blocked'] else '%d ms' % r['dwm_flush_ms']})"
                 + (" przy ~0% obciążenia 3D GPU - zawieszony kompozytor / ścieżka prezentacji sterownika, nie brak mocy"
                    if gpu_idle else ""))
    if shell_bad:
        v.append(f"Explorer nie odpowiada ({r['shell_ping_ms']:.0f} ms)")
    elif dwm_bad:
        v.append("Explorer odpowiada - system i powłoka żyją, problem jest w warstwie grafiki (DWM/sterownik/MPO)")
    if r["foreground"]["hung"]:
        v.append(f"Okno na pierwszym planie zawieszone: {r['foreground']['exe']}")
    rates = {d["mode"].split("@")[-1] for d in r["displays"] if "@" in d["mode"]}
    adapters = {d["adapter"] for d in r["displays"]}
    if len(rates) > 1:
        v.append("Monitory z różnym odświeżaniem: " + ", ".join(sorted(rates)))
    if len(adapters) > 1:
        v.append("Monitory podłączone do różnych GPU: " + ", ".join(sorted(adapters)))
    if r["cpu_pct"] > 90:
        v.append(f"CPU {r['cpu_pct']:.0f}% - sprawdź top procesy")
    if r["ram_pct"] > 90:
        v.append(f"RAM {r['ram_pct']:.0f}%")
    for p in r["dwm_process"]:
        if p["age_s"] < 600:
            v.append(f"dwm.exe uruchomiony {p['age_s']} s temu - DWM się zrestartował")
    if not dwm_bad and not shell_bad and not r["foreground"]["hung"]:
        v.append("Teraz wszystko odpowiada - zamrożenia nie widać w tej chwili")
    return v
