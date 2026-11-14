r"""Kernel dumps Windows writes without a clear event-log entry: LiveKernelReports (GPU driver timeouts, win32k and
USB watchdogs: the system keeps running) plus Minidump / MEMORY.DMP (blue screens).

No debugger needed: the type comes from the folder (LiveKernelReports\WATCHDOG = display driver timeouts) and the
bug check code from the dump header ("PAGEDU64": BugCheckCode at 0x38, parameters at 0x40). The folders need
admin rights (the collector runs elevated). Human-readable strings are Polish."""
import json
import os
import struct
import time

from . import db

LKR = r"C:\Windows\LiveKernelReports"
MINIDUMP = r"C:\Windows\Minidump"
MEMORY_DMP = r"C:\Windows\MEMORY.DMP"
SEEN_KEY = "kdumps_seen"
SCAN_EVERY_S = 300
SETTLE_S = 60              # a dump modified this recently may still be written (MEMORY.DMP is copied out at boot)

BUGCHECKS = {
    0x0A: "IRQL_NOT_LESS_OR_EQUAL", 0x1A: "MEMORY_MANAGEMENT", 0x1E: "KMODE_EXCEPTION_NOT_HANDLED",
    0x3B: "SYSTEM_SERVICE_EXCEPTION", 0x50: "PAGE_FAULT_IN_NONPAGED_AREA", 0x7E: "SYSTEM_THREAD_EXCEPTION_NOT_HANDLED",
    0x9F: "DRIVER_POWER_STATE_FAILURE", 0xA0: "INTERNAL_POWER_ERROR", 0xD1: "DRIVER_IRQL_NOT_LESS_OR_EQUAL",
    0xEF: "CRITICAL_PROCESS_DIED", 0x101: "CLOCK_WATCHDOG_TIMEOUT", 0x113: "VIDEO_DXGKRNL_FATAL_ERROR",
    0x116: "VIDEO_TDR_FAILURE", 0x117: "VIDEO_TDR_TIMEOUT_DETECTED", 0x119: "VIDEO_SCHEDULER_INTERNAL_ERROR",
    0x124: "WHEA_UNCORRECTABLE_ERROR", 0x133: "DPC_WATCHDOG_VIOLATION", 0x139: "KERNEL_SECURITY_CHECK_FAILURE",
    0x141: "VIDEO_ENGINE_TIMEOUT_DETECTED", 0x144: "BUGCODE_USB3_DRIVER", 0x15F: "CONNECTED_STANDBY_WATCHDOG_TIMEOUT_LIVEDUMP",
    0x193: "VIDEO_DXGKRNL_LIVEDUMP", 0x19C: "WIN32K_POWER_WATCHDOG_TIMEOUT", 0x1A1: "WIN32K_CALLOUT_WATCHDOG_LIVEDUMP",
    0x1A2: "WIN32K_CALLOUT_WATCHDOG_BUGCHECK", 0x1B0: "VIDEO_MINIPORT_FAILED_LIVEDUMP",
    0x1CC: "EXRESOURCE_TIMEOUT_LIVEDUMP", 0x1D4: "UCMUCSI_LIVEDUMP",
}
GPU_CODES = {0x113, 0x116, 0x117, 0x119, 0x141, 0x193, 0x1B0}
# LiveKernelReports subfolder -> what it means
FOLDERS = {
    "watchdog": ("gpu", "timeout sterownika GPU (TDR) - karta przestała odpowiadać, sterownik ją zresetował"),
    "pow32kwatchdog": ("win32k", "watchdog win32k - wątek grafiki/zasilania zawiesił się"),
    "usbhub3": ("usb", "reset huba/urządzenia USB"),
    "ndis": ("network", "zawieszenie sterownika sieci"),
    "pdcrevocation": ("power", "watchdog zasilania (Modern Standby)"),
    "dxgkrnl": ("gpu", "zrzut jądra grafiki (DirectX)"),
}


def read_header(path):
    """(bug check code, [params]) from a kernel dump header, or (None, None) for an unknown/unreadable format."""
    try:
        with open(path, "rb") as f:
            h = f.read(0x60)
    except OSError:
        return None, None
    if h[:8] == b"PAGEDU64" and len(h) >= 0x60:
        return struct.unpack_from("<I", h, 0x38)[0], list(struct.unpack_from("<4Q", h, 0x40))
    if h[:8] == b"PAGEDUMP" and len(h) >= 0x3C:
        return struct.unpack_from("<I", h, 0x28)[0], list(struct.unpack_from("<4I", h, 0x2C))
    return None, None


def classify(path, code):
    """(bucket, source, description) from the folder and the bug check code."""
    p = path.lower()
    if p.startswith(MINIDUMP.lower()) or p == MEMORY_DMP.lower():
        source = "bsod"
        bucket, desc = ("gpu", "blue screen sterownika grafiki") if code in GPU_CODES else ("bsod", "blue screen")
    else:
        source = "live"
        parts = os.path.relpath(path, LKR).lower().split(os.sep)
        folder = parts[0] if len(parts) > 1 else ""
        bucket, desc = FOLDERS.get(folder, ("other", "zrzut jądra na żywo (system działał dalej)"))
        if code in GPU_CODES and bucket != "gpu":
            bucket, desc = "gpu", FOLDERS["watchdog"][1]
        if bucket == "other":   # unknown folder: the file name often carries the component (e.g. WATCHDOG-...)
            name = os.path.basename(p)
            hit = next((k for k in FOLDERS if name.startswith(k)), None)
            if hit:
                bucket, desc = FOLDERS[hit]
    return bucket, source, desc


def list_dumps():
    """[(path, size, mtime)] of every kernel dump on disk (silently empty without admin rights)."""
    out = []
    for root in (LKR, MINIDUMP):
        for d, _, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".dmp"):
                    p = os.path.join(d, f)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    out.append((p, st.st_size, st.st_mtime))
    try:
        st = os.stat(MEMORY_DMP)
        out.append((MEMORY_DMP, st.st_size, st.st_mtime))
    except OSError:
        pass
    return out


def nearest_session(con, ts):
    """(session id, exe, 'during'|'after') of the game/stress session running at ts or ended up to 10 min before."""
    r = con.execute("SELECT id, exe, start_ts, end_ts FROM sessions WHERE start_ts<=? AND COALESCE(end_ts, ?)>=? "
                    "ORDER BY start_ts DESC LIMIT 1", (ts + 60, ts, ts - 600)).fetchone()
    if not r:
        return None, None, None
    return r[0], r[1], "during" if r[3] is None or r[3] >= ts - 60 else "after"


def describe(path, size, mtime, con=None):
    code, params = read_header(path)
    bucket, source, desc = classify(path, code)
    name = BUGCHECKS.get(code) if code is not None else None
    det = {"path": path, "size_mb": round(size / 2**20, 1), "source": source, "bucket": bucket, "description": desc,
           "bugcheck": f"0x{code:X}" if code is not None else None, "bugcheck_name": name,
           "params": [f"0x{p:X}" for p in params] if params else None,
           "folder": os.path.basename(os.path.dirname(path))}
    if con is not None:
        det["session"], det["exe"], det["when"] = nearest_session(con, mtime)
    sev = "critical" if source == "bsod" else "serious" if bucket in ("gpu", "win32k") else "warning"
    what = f"0x{code:X} {name}" if name else (f"0x{code:X}" if code is not None else det["folder"])
    title = ("Blue screen - zrzut pamięci" if source == "bsod" else "Zrzut jądra na żywo") + f": {what} ({desc})"
    if det.get("exe"):
        title += f", {'w trakcie' if det['when'] == 'during' else 'po'} {det['exe']}"
    return sev, title, det


def scan(con, now=None):
    """New dumps since the last scan -> [(severity, title, details, ts)]. Remembers path|mtime of every dump still on
    disk (Windows caps LiveKernelReports itself), so a rewritten MEMORY.DMP counts as new and deleted ones drop out."""
    now = now or time.time()
    try:
        seen = set(json.loads(db.get_meta(con, SEEN_KEY, "[]")))
    except ValueError:
        seen = set()
    found = [d for d in list_dumps() if now - d[2] >= SETTLE_S]
    keys = {p: f"{p}|{int(mtime)}" for p, _, mtime in found}
    out = [(*describe(p, size, mtime, con), mtime) for p, size, mtime in found if keys[p] not in seen]
    if set(keys.values()) != seen:
        db.set_meta(con, SEEN_KEY, json.dumps(sorted(keys.values())))
    return out


# ---------- analysis ----------
def findings(con, t0, unclean=None, crash_dump=None):
    """`pm health` findings: kernel dumps by bucket, and why there are no blue-screen dumps for the resets."""
    f = []
    rows = con.execute("SELECT details FROM incidents WHERE kind='kernel_dump' AND ts>=?", (t0,)).fetchall()
    dets = []
    for (d,) in rows:
        try:
            dets.append(json.loads(d or "{}"))
        except ValueError:
            pass
    gpu = [d for d in dets if d.get("bucket") == "gpu" and d.get("source") == "live"]
    if gpu:
        in_game = sum(1 for d in gpu if d.get("session"))
        f.append({"severity": "serious", "title": f"{len(gpu)}x timeout sterownika GPU (LiveKernelReports)",
                  "detail": f"Windows zapisał zrzuty WATCHDOG ({in_game} w trakcie lub tuż po grze): karta przestała "
                            "odpowiadać i sterownik ją zresetował, bez wpisu w dzienniku. Przyczyny: niestabilny UV/OC GPU "
                            "(cofnij na test), sterownik Adrenalin (czysta instalacja DDU), zasilanie karty."})
    other = [d for d in dets if d.get("source") == "live" and d.get("bucket") != "gpu"]
    if other:
        kinds = sorted({d.get("description") for d in other if d.get("description")})
        f.append({"severity": "warning", "title": f"{len(other)}x zrzut jądra na żywo",
                  "detail": "; ".join(kinds[:4]) + ". System działał dalej - ścieżki w pm incidents --kind kernel_dump."})
    no_bsod = [u for u in unclean or [] if u.get("bugcheck_code") == 0]
    bsod_dumps = [d for d in dets if d.get("source") == "bsod"]
    if no_bsod and not bsod_dumps:
        cd = crash_dump or {}
        why = ("BugcheckCode=0 w Kernel-Power 41: system nie zgłosił błędu, tylko stracił zasilanie lub zawiesił się "
               "sprzętowo - wtedy Windows nie ma kiedy zapisać zrzutu, więc pusty C:\\Windows\\Minidump jest oczekiwany.")
        if cd.get("problems"):
            why += " Dodatkowo konfiguracja zrzutów: " + "; ".join(cd["problems"]) + "."
        f.append({"severity": "info", "title": "Brak zrzutów blue screena przy resetach", "detail": why})
    return f


def latest_crash_dump(con):
    """crash_dump section of the newest config snapshot (dumpconfig.read via sysinfo), or None."""
    r = con.execute("SELECT info FROM system_info ORDER BY ts DESC LIMIT 1").fetchone()
    try:
        return (json.loads(r[0]) or {}).get("crash_dump") if r else None
    except (ValueError, AttributeError):
        return None
