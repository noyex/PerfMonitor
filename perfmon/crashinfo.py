"""Crash details for game sessions: faulting module, exception code, WER report, dumps and an interpretation.

Sources: Application Error 1000 / WER 1001 (events.data = named EventData, or [Name=val] appended to the
message for older rows, or the localized text as a last resort), Report.wer in WER\\ReportArchive (admin),
%LOCALAPPDATA%\\CrashDumps (only when WER LocalDumps is enabled - `pm dumps enable`)."""
import glob
import json
import os
import re
import subprocess
import time
import winreg

from . import analyze as an

WER_DIRS = [r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive", r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue"]
CRASH_DUMPS = os.path.expandvars(r"%LOCALAPPDATA%\CrashDumps")
MINIDUMP = r"C:\Windows\Minidump"
CDB = r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe"
LOCAL_DUMPS_KEY = r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps"

# Application Error 1000 EventData order when the fields are unnamed
AE1000_ORDER = ["AppName", "AppVersion", "AppTimeStamp", "ModuleName", "ModuleVersion", "ModuleTimeStamp",
                "ExceptionCode", "FaultingOffset", "ProcessId", "ProcessCreationTime", "IntegratorReportId",
                "PackageFullName", "PackageRelativeAppId"]

EXCEPTIONS = {
    0xC0000005: "ACCESS_VIOLATION", 0xC0000006: "IN_PAGE_ERROR", 0xC0000017: "NO_MEMORY",
    0xC000001D: "ILLEGAL_INSTRUCTION", 0xC000008E: "FLOAT_DIVIDE_BY_ZERO", 0xC0000094: "INTEGER_DIVIDE_BY_ZERO",
    0xC0000096: "PRIVILEGED_INSTRUCTION", 0xC00000FD: "STACK_OVERFLOW", 0xC0000374: "HEAP_CORRUPTION",
    0xC0000409: "STACK_BUFFER_OVERRUN (fail fast)", 0xC000041D: "FATAL_USER_CALLBACK_EXCEPTION",
    0xC0000602: "FAIL_FAST_EXCEPTION", 0x80000003: "BREAKPOINT", 0x40000015: "FATAL_APP_EXIT (abort)",
    0xE0434352: "CLR_EXCEPTION (.NET)", 0xE06D7363: "C++ exception (unhandled)",
    0x887A0005: "DXGI_ERROR_DEVICE_REMOVED", 0x887A0006: "DXGI_ERROR_DEVICE_HUNG", 0x887A0007: "DXGI_ERROR_DEVICE_RESET",
}
GPU_MODULES = {"amdxc64.dll", "amdxx64.dll", "amdvlk64.dll", "atidxx64.dll", "amdihk64.dll", "amdenc64.dll",
               "amduve64.dll", "nvwgf2umx.dll", "nvoglv64.dll", "nvlddmkm.sys", "d3d12core.dll", "d3d12.dll",
               "dxgi.dll", "d3d11.dll", "vulkan-1.dll"}
OVERLAY_MODULES = {"rtsshooks64.dll", "discordhook64.dll", "gameoverlayrenderer64.dll", "overlay64.dll",
                   "medal-hook64.dll", "obs-vulkan64.dll", "graphics-hook64.dll", "owclient.dll", "igo64.dll"}
DXGI_CODES = {0x887A0005, 0x887A0006, 0x887A0007}
CPU_SUSPECT_CODES = {0xC0000409, 0xC000001D, 0xC0000096, 0xC000008E, 0xC0000094}
SYSTEM_MODULES = {"ntdll.dll", "kernelbase.dll", "kernel32.dll", "ucrtbase.dll", "msvcp140.dll", "vcruntime140.dll",
                  "combase.dll", "unknown", ""}
ENGINE_MODULES = re.compile(r"(-win64-shipping\.exe|unityplayer\.dll|gameassembly\.dll|^cod\.exe$|^cs2\.exe$|"
                            r"engine2\.dll|client\.dll|server\.dll)$", re.I)

_BRACKET = re.compile(r"\[([^\[\]]*=[^\[\]]*)\]\s*$", re.S)
_KV = re.compile(r"(\w+)=([^;]*)(?:;|$)")


def _data(ev):
    """Named fields of an event: events.data dict, positional list (AE 1000 order) or [Name=val] in the message."""
    d = ev.get("data")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = None
    if isinstance(d, dict):
        return d
    if isinstance(d, list) and ev.get("provider") == "Application Error":
        return dict(zip(AE1000_ORDER, d))
    m = _BRACKET.search(ev.get("message") or "")
    if m:
        return {k: v.strip() for k, v in _KV.findall(m.group(1))}
    return {}


def _code(v):
    if v in (None, ""):
        return None
    try:
        return int(str(v).strip(), 16) & 0xFFFFFFFF
    except ValueError:
        return None


def code_name(code):
    return EXCEPTIONS.get(code) if code is not None else None


def fmt_code(code):
    return f"0x{code:08X}" if code is not None else None


def parse_app_error(ev):
    """Application Error 1000 -> {app, app_version, module, module_version, exception_code, offset, report_id, ...}."""
    d = _data(ev)
    out = {"app": d.get("AppName"), "app_version": d.get("AppVersion"), "module": d.get("ModuleName"),
           "module_version": d.get("ModuleVersion"), "exception_code": _code(d.get("ExceptionCode")),
           "offset": d.get("FaultingOffset"), "report_id": d.get("IntegratorReportId") or d.get("ReportId"),
           "app_path": d.get("AppPath"), "module_path": d.get("ModulePath")}
    if not out["exception_code"] or not out["module"]:   # localized text only (old rows without fields)
        m = ev.get("message") or ""
        if not out["exception_code"]:
            c = re.search(r"(?:wyj\S*tku|exception code)\s*:\s*(0x[0-9a-f]{8})", m, re.I) or re.search(r"\b(0x[cCeE8][0-9a-fA-F]{7})\b", m)
            out["exception_code"] = _code(c.group(1)) if c else None
        if not out["module"]:
            c = re.search(r"(?:modu\S*|module name)[^:\r\n]*:\s*([^\s,]+\.(?:dll|exe|sys))", m, re.I)
            out["module"] = c.group(1) if c else None
    return out


def parse_wer(ev):
    """WER 1001 -> {event_name, p: [P1..P10], report_path, report_id, attached}."""
    d = _data(ev)
    store = (d.get("StorePath") or "").strip()
    if not store:
        m = re.search(r"(\\\\\?\\[A-Z]:\\ProgramData\\Microsoft\\Windows\\WER\\Report\w+\\[^\r\n\[;]+)", ev.get("message") or "")
        store = m.group(1).strip() if m else ""
    return {"event_name": d.get("EventName"), "p": [d.get(f"P{i}") for i in range(1, 11)],
            "report_path": store.replace("\\\\?\\", "") or None, "report_id": d.get("ReportId"),
            "attached": [a.strip() for a in (d.get("AttachedFiles") or "").splitlines() if a.strip()]}


def wer_crash_fields(w):
    """APPCRASH: P1 app P4 module P7 code P8 offset; BEX/BEX64 (fail fast): P4 module P7 offset P8 code."""
    p = w["p"] + [None] * 10
    if (w.get("event_name") or "").upper().startswith("BEX"):
        return {"app": p[0], "app_version": p[1], "module": p[3], "module_version": p[4], "offset": p[6],
                "exception_code": _code(p[7])}
    if (w.get("event_name") or "").upper() in ("APPCRASH", "MOAPPCRASH"):
        return {"app": p[0], "app_version": p[1], "module": p[3], "module_version": p[4],
                "exception_code": _code(p[6]), "offset": p[7]}
    return {}


def read_report_wer(path):
    """Report.wer (UTF-16 'key=value' lines) -> {friendly_event_name, app_path, sig: {name: value}, dynamic_sig, ui2}."""
    f = os.path.join(path, "Report.wer") if os.path.isdir(path) else path
    try:
        with open(f, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    txt = raw.decode("utf-16", errors="replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", errors="replace")
    kv = {}
    for ln in txt.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            kv[k.strip()] = v.strip()

    def pairs(prefix):
        out, i = {}, 0
        while f"{prefix}[{i}].Name" in kv or f"{prefix}[{i}].Value" in kv:
            out[kv.get(f"{prefix}[{i}].Name") or f"{prefix}{i}"] = kv.get(f"{prefix}[{i}].Value")
            i += 1
        return out
    return {"friendly_event_name": kv.get("FriendlyEventName"), "event_type": kv.get("EventType"),
            "app_path": kv.get("AppPath"), "app_name": kv.get("AppName"), "sig": pairs("Sig"),
            "dynamic_sig": pairs("DynamicSig"), "ui2": kv.get("UI[2]"), "report_path": os.path.dirname(f)}


def find_report_dir(exe, end_ts, report_id=None, window_s=600):
    """Newest WER report dir for this exe (by report id in the dir name, else name + mtime within window_s)."""
    stem = (exe or "").lower()[:15]   # WER truncates the app name in the dir name
    best = None
    for root in WER_DIRS:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            nl = n.lower()
            if report_id and report_id.lower() in nl:
                return os.path.join(root, n)
            if not (nl.startswith(("appcrash_", "bex64_", "bex_", "moappcrash_")) and f"_{stem}" in nl):
                continue
            p = os.path.join(root, n)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if abs(mt - end_ts) <= window_s and (best is None or mt > best[0]):
                best = (mt, p)
    return best[1] if best else None


def new_dumps(exe, since_ts, dirs=(CRASH_DUMPS,)):
    """Dump files for exe created since since_ts (LocalDumps names them <exe>.<pid>.dmp)."""
    out = []
    for d in dirs:
        for p in glob.glob(os.path.join(d, f"{exe}*.dmp")):
            try:
                if os.path.getmtime(p) >= since_ts - 5:
                    out.append(p)
            except OSError:
                pass
    return sorted(out, key=os.path.getmtime)


# ---------- interpretation ----------
def interpret(code, module, exe, history=None):
    """{verdict, confidence, next_steps}. history: [(code, module)] of recent crashes of the same exe (this one incl.)."""
    m = (module or "").lower()
    e = (exe or "").lower()
    history = history or []
    modules = {(hm or "").lower() for _, hm in history if hm}
    unstable_codes = [c for c, _ in history if c in CPU_SUSPECT_CODES | {0xC0000005}]
    if code in DXGI_CODES:
        return {"verdict": "GPU przestało odpowiadać (TDR / device removed): sterownik, undervolt/OC GPU, zasilanie "
                           "karty albo temperatura hotspot/VRAM", "confidence": "high",
                "next_steps": ["Cofnij undervolt/OC GPU do ustawień fabrycznych i sprawdź, czy crash wraca",
                               "Sprawdź temperatury hotspot/VRAM tuż przed crashem: pm at <czas> --window 60",
                               "Czysta instalacja sterownika (DDU) lub inna wersja Adrenalin",
                               "Kable zasilania karty: osobne przewody PCIe, bez rozgałęźników"]}
    if len(modules - SYSTEM_MODULES) >= 3 and len(unstable_codes) >= 3:
        return {"verdict": f"Podejrzenie niestabilnego CPU/RAM: {len(history)} crashy w {len(modules)} różnych modułach",
                "confidence": "medium",
                "next_steps": ["Wyłącz na test EXPO / Curve Optimizer / PBO i zagraj tak samo długo",
                               "Sprawdź błędy WHEA (pm incidents --kind whea_corrected, --kind whea_fatal) i stabilność w pm oc",
                               "Test RAM: MemTest86 / TestMem5, test CPU: OCCT / y-cruncher"]}
    if m in GPU_MODULES:
        return {"verdict": f"Crash w sterowniku/API grafiki ({module}): sterownik GPU lub nakładka; sprawdź też undervolt GPU",
                "confidence": "medium",
                "next_steps": ["Wyłącz nakładki (Adrenalin, Discord, Steam, RTSS) i undervolt GPU na test",
                               "Zaktualizuj lub cofnij sterownik Adrenalin (czysta instalacja przez DDU)",
                               "Jeśli powtarza się w wielu grach - podejrzenie stabilności GPU (UV za nisko)"]}
    if m in OVERLAY_MODULES:
        return {"verdict": f"Crash w module nakładki ({module})", "confidence": "high",
                "next_steps": [f"Wyłącz nakładkę, która ładuje {module}, albo ją zaktualizuj"]}
    if code == 0xE0434352:
        return {"verdict": "Wyjątek .NET - zwykle launcher/narzędzie, nie sam silnik gry", "confidence": "high",
                "next_steps": ["Zaktualizuj/przeinstaluj launcher; sprawdź zdarzenia '.NET Runtime 1026' w tym czasie"]}
    if code == 0xC0000374:
        return {"verdict": "Uszkodzenie sterty: bug gry albo wstrzykiwanej nakładki (RTSS, Discord, Medal, OBS)",
                "confidence": "medium",
                "next_steps": ["Wyłącz nakładki i narzędzia wstrzykujące się do gry", "Zweryfikuj pliki gry",
                               "Jeśli crash wraca w różnych grach - test stabilności RAM"]}
    if code == 0xC0000006:
        return {"verdict": "Błąd stronicowania (IN_PAGE_ERROR): nie udało się wczytać pliku gry / pliku wymiany z dysku",
                "confidence": "medium",
                "next_steps": ["Sprawdź zdrowie dysku z grą (pm sysinfo) i zdarzenia disk/stornvme",
                               "Zweryfikuj pliki gry", "Plik wymiany na szybkim dysku, zarządzany przez system"]}
    if code == 0xC0000005 and (ENGINE_MODULES.search(m) or (m and m == e) or (m.endswith(".exe") and m not in SYSTEM_MODULES)):
        return {"verdict": "Błąd w kodzie gry/silnika (ACCESS_VIOLATION w module gry): bug gry albo uszkodzone pliki",
                "confidence": "medium",
                "next_steps": ["Zweryfikuj pliki gry (Steam/Battle.net: Sprawdź i napraw)",
                               "Wyczyść cache shaderów gry", "Jeśli crash pojawił się po aktualizacji - zgłoś/poczekaj na patch"]}
    if code in CPU_SUSPECT_CODES:
        return {"verdict": f"{code_name(code)}: program sam się przerwał. Błąd programu, a jeśli to się powtarza w różnych "
                           "modułach - niestabilny CPU/RAM", "confidence": "low",
                "next_steps": ["Porównaj moduły kolejnych crashy (pm health)",
                               "Przy powtórkach: test bez EXPO/CO, sprawdź WHEA"]}
    if m in SYSTEM_MODULES and code in (0xC0000005, None):
        return {"verdict": f"Crash w bibliotece systemowej ({module or 'nieznany moduł'}) - przyczyna nieokreślona; "
                           "najczęściej nakładki lub uszkodzone pliki gry", "confidence": "low",
                "next_steps": ["Wyłącz nakładki", "Zweryfikuj pliki gry",
                               "Włącz minidumpy (pm dumps enable), żeby przy kolejnym crashu zobaczyć stos"]}
    return {"verdict": "Przyczyna nieokreślona", "confidence": "low",
            "next_steps": ["Zbierz więcej crashy; włącz minidumpy (pm dumps enable)"]}


# ---------- building summary.crash ----------
def _events(con, exe, t0, t1):
    return an._rows(con, "SELECT ts, log, provider, event_id, message, data FROM events WHERE ts BETWEEN ? AND ? AND "
                         "((provider='Application Error' AND event_id=1000) OR (provider='Windows Error Reporting' "
                         "AND event_id=1001) OR (provider='Application Hang' AND event_id=1002)) AND message LIKE ? "
                         "ORDER BY ts", (t0, t1, f"%{exe}%"))


def history(con, exe, before_ts, days=30, limit=10):
    """[(code, module)] of the last crashes of exe (Application Error 1000)."""
    out = []
    for ev in an._rows(con, "SELECT provider, message, data FROM events WHERE provider='Application Error' AND event_id=1000 "
                            "AND ts BETWEEN ? AND ? AND message LIKE ? ORDER BY ts DESC LIMIT ?",
                       (before_ts - days * 86400, before_ts + 120, f"%{exe}%", limit)):
        f = parse_app_error(ev)
        if (f.get("app") or exe).lower() == exe.lower():
            out.append((f["exception_code"], f["module"]))
    return out


def build(con, exe, end_ts, start_ts=None, late_s=75, read_files=True):
    """summary.crash for a session of exe that ended at end_ts, or None when no crash event is found."""
    evs = _events(con, exe, end_ts - 90, end_ts + late_s)
    ae = next((e for e in evs if e["event_id"] == 1000 and (parse_app_error(e).get("app") or "").lower() == exe.lower()), None)
    hang = next((e for e in evs if e["event_id"] == 1002), None)
    wer = next((e for e in evs if e["event_id"] == 1001 and (parse_wer(e)["p"][0] or "").lower() == exe.lower()), None)
    if not (ae or hang or wer):
        return None
    c = {}
    if ae:
        c.update({k: v for k, v in parse_app_error(ae).items() if v is not None})
    if wer:
        w = parse_wer(wer)
        for k, v in wer_crash_fields(w).items():
            c.setdefault(k, v)
        c["wer_event"] = w["event_name"]
        if w["report_path"]:
            c["report_path"] = w["report_path"]
    if hang and not ae:
        d = _data(hang)
        c.update({"hang": True, "hang_type": d.get("HangType"), "app_version": d.get("AppVersion")})
    if read_files:
        rdir = c.get("report_path") if c.get("report_path") and os.path.isdir(c["report_path"]) else \
            find_report_dir(exe, end_ts, c.get("report_id"))
        rep = read_report_wer(rdir) if rdir else None
        if rep:
            c["report_path"] = rep["report_path"]
            c["report"] = {k: v for k, v in rep.items() if v and k != "report_path"}
        dumps = new_dumps(exe, start_ts or end_ts - 3600)
        if dumps:
            c["dump_path"] = dumps[-1]
    code = c.get("exception_code")
    c["exception_name"] = code_name(code)
    if code is not None:
        c["exception_code"] = fmt_code(code)
    if c.get("hang"):
        c["interpretation"] = {"verdict": "Gra przestała odpowiadać i została zamknięta (Application Hang)",
                               "confidence": "medium",
                               "next_steps": ["Sprawdź, co działo się tuż przed: pm at <czas> --window 60 (dysk, GPU, DWM)",
                                              "Jeśli zamknąłeś grę sam po zawieszeniu - to zawieszenie, nie crash"]}
    else:
        c["interpretation"] = interpret(code, c.get("module"), exe, history(con, exe, end_ts))
    return c


def finding(crash, exe):
    """(title, detail) for the session findings."""
    if not crash:
        return (f"{exe} zakończył się crashem",
                "Sesja zakończyła się awarią gry - nie znaleziono szczegółów (zdarzenia 1000/1001) w dzienniku.")
    it = crash.get("interpretation") or {}
    where = ", ".join(x for x in (crash.get("module") and f"moduł {crash['module']}",
                                  crash.get("exception_code") and f"kod {crash['exception_code']}"
                                  + (f" ({crash['exception_name']})" if crash.get("exception_name") else "")) if x)
    steps = "; ".join(it.get("next_steps") or [])
    if crash.get("hang"):
        return (f"{exe} przestał odpowiadać i został zamknięty (Application Hang)",
                f"{it.get('verdict', '')}. Kroki: {steps}")
    return (f"{exe} zakończył się crashem" + (f": {where}" if where else ""),
            f"{it.get('verdict', '')} [pewność: {it.get('confidence', '?')}]. Kroki: {steps}")


def annotate_recurring(con, crashes, t0):
    """Add module / code / verdict of the latest crash to pm health's recurring crash rows."""
    for c in crashes:
        if c.get("kind") != "app_crash":
            continue
        app = c.get("app") or ""
        hist = history(con, app, time.time(), days=(time.time() - t0) / 86400, limit=20)
        if not hist:
            continue
        code, module = hist[0]
        c["module"], c["code"] = module, fmt_code(code)
        c["modules"] = len({(m or "").lower() for _, m in hist if m})
        c["verdict"] = interpret(code, module, app, hist)["verdict"]
    return crashes


# ---------- BSOD (optional, only if Debugging Tools are installed) ----------
def minidump_near(ts, window_s=3600):
    """Minidump written closest to a BSOD event at ts (the dump and BugCheck 1001 are both written at the next boot)."""
    best = None
    for p in glob.glob(os.path.join(MINIDUMP, "*.dmp")):
        try:
            d = abs(os.path.getmtime(p) - ts)
        except OSError:
            continue
        if d <= window_s and (best is None or d < best[0]):
            best = (d, p)
    return best[1] if best else None


def bsod_cause(dump=None, timeout=180):
    """'Probably caused by' from `cdb !analyze -v` on the newest minidump; None when cdb or a dump is missing."""
    if not os.path.exists(CDB):
        return None
    if dump is None:
        dumps = sorted(glob.glob(os.path.join(MINIDUMP, "*.dmp")), key=os.path.getmtime)
        if not dumps:
            return None
        dump = dumps[-1]
    try:
        r = subprocess.run([CDB, "-z", dump, "-c", "!analyze -v; q"], capture_output=True, text=True, timeout=timeout,
                           errors="replace", creationflags=0x08000000)
    except Exception:
        return None
    m = re.search(r"Probably caused by\s*:\s*(\S+)", r.stdout or "")
    b = re.search(r"BUGCHECK_CODE:\s*(\S+)", r.stdout or "")
    return {"dump": dump, "probably_caused_by": m.group(1) if m else None, "bugcheck": b.group(1) if b else None}


# ---------- WER LocalDumps (user-mode minidumps) ----------
def local_dumps_status():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LOCAL_DUMPS_KEY) as k:
            vals = {}
            for n in ("DumpType", "DumpCount", "DumpFolder"):
                try:
                    vals[n] = winreg.QueryValueEx(k, n)[0]
                except OSError:
                    pass
            return {"enabled": True, **vals}
    except OSError:
        return {"enabled": False}


def set_local_dumps(enable):
    """Needs admin (HKLM) - called by the collector on a `pm dumps` request only."""
    if enable:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, LOCAL_DUMPS_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "DumpType", 0, winreg.REG_DWORD, 1)    # minidump
            winreg.SetValueEx(k, "DumpCount", 0, winreg.REG_DWORD, 5)
            winreg.SetValueEx(k, "DumpFolder", 0, winreg.REG_EXPAND_SZ, r"%LOCALAPPDATA%\CrashDumps")
    else:
        try:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, LOCAL_DUMPS_KEY)
        except FileNotFoundError:
            pass
    return local_dumps_status()
