"""DLLs loaded into the game process: overlays, capture hooks, injectors and anti-cheat (read-only).

One Toolhelp32 module snapshot (CreateToolhelp32Snapshot TH32CS_SNAPMODULE) after the game settled and again on a
slow cadence; nothing is opened for writing and no code is injected. Kernel anti-cheats (Ricochet, Javelin,
Vanguard, ...) strip access to their game: then the result is "unknown" with the Windows error, never "none".
Only the flagged modules are stored (plus counts): a game loads hundreds of DLLs."""
import ctypes
import os
from ctypes import wintypes

# lowercase name prefix -> (category, label)
KNOWN = {
    "discordhook": ("overlay", "Discord"),
    "gameoverlayrenderer": ("overlay", "Steam overlay"),
    "overwolf": ("overlay", "Overwolf"), "ow-graphics": ("overlay", "Overwolf"), "owclient": ("overlay", "Overwolf"),
    "rtsshooks": ("overlay", "RTSS / Afterburner"), "rtsscore": ("overlay", "RTSS / Afterburner"),
    "msiafterburner": ("overlay", "MSI Afterburner"),
    "medal": ("capture", "Medal"),
    "amdovlay": ("overlay", "Adrenalin overlay"), "amdoverlay": ("overlay", "Adrenalin overlay"),
    "nvspcap": ("capture", "NVIDIA overlay"), "nvcamera": ("overlay", "NVIDIA overlay"),
    "gamebarpresencewriter": ("overlay", "Xbox Game Bar"), "gamechatoverlayext": ("overlay", "Xbox Game Bar"),
    "graphics-hook": ("capture", "OBS game capture"),
    "reshade": ("injector", "ReShade"), "specialk": ("injector", "Special K"),
    "faceit": ("anticheat", "FACEIT"),
    "easyanticheat": ("anticheat", "EasyAntiCheat"), "eosanticheat": ("anticheat", "EasyAntiCheat"),
    "beclient": ("anticheat", "BattlEye"),
    "eaanticheat": ("anticheat", "EA Javelin"),
}
# graphics API DLLs that, loaded from outside Windows, are a proxy/injector (ReShade, ENB, mods)
PROXY = {"dxgi.dll", "d3d9.dll", "d3d11.dll", "d3d12.dll", "dinput8.dll", "opengl32.dll", "winmm.dll", "version.dll"}
SYSTEM_DIRS = ("\\windows\\system32\\", "\\windows\\syswow64\\", "\\windows\\winsxs\\", "\\driverstore\\")


class _ME32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD), ("hModule", ctypes.c_void_p), ("szModule", ctypes.c_wchar * 256),
                ("szExePath", ctypes.c_wchar * 260)]


def modules(pid):
    """[(name, path)] or raises OSError(winerror) (5 = access denied: protected process)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.Module32FirstW.argtypes = k32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ME32)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    for _ in range(3):   # ERROR_BAD_LENGTH (24): the module list changed during the snapshot, retry
        h = k32.CreateToolhelp32Snapshot(0x8 | 0x10, pid)   # TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32
        if h not in (None, ctypes.c_void_p(-1).value):
            break
        err = ctypes.get_last_error()
        if err != 24:
            raise OSError(err, ctypes.FormatError(err).strip())
    else:
        raise OSError(24, "module list kept changing")
    out = []
    try:
        me = _ME32()
        me.dwSize = ctypes.sizeof(_ME32)
        ok = k32.Module32FirstW(h, ctypes.byref(me))
        while ok:
            out.append((me.szModule, me.szExePath))
            ok = k32.Module32NextW(h, ctypes.byref(me))
    finally:
        k32.CloseHandle(h)
    return out


def classify(mods):
    flagged = {}
    for name, path in mods:
        low, lp = name.lower(), (path or "").lower()
        hit = next((v for k, v in KNOWN.items() if low.startswith(k)), None)
        if hit is None and low in PROXY and lp and not any(d in lp for d in SYSTEM_DIRS):
            hit = ("injector", f"proxy {name}")
        if hit:
            flagged.setdefault(hit[1], {"name": hit[1], "category": hit[0], "dll": name,
                                        "path": os.path.dirname(path) if path else None})
    return sorted(flagged.values(), key=lambda x: (x["category"], x["name"]))


def scan(pid):
    """{"status": ok|unknown, "count", "flagged": [...], "categories": {category: n}} or unknown with the reason."""
    try:
        mods = modules(pid)
    except OSError as e:
        return {"status": "unknown", "reason": f"brak dostępu do modułów procesu (błąd {e.errno}: {e.strerror})"
                if e.errno == 5 else f"błąd {e.errno}: {e.strerror}"}
    if not mods:
        return {"status": "unknown", "reason": "pusta lista modułów (proces chroniony lub zakończony)"}
    fl = classify(mods)
    cats = {}
    for f in fl:
        cats[f["category"]] = cats.get(f["category"], 0) + 1
    return {"status": "ok", "count": len(mods), "flagged": fl, "categories": cats}


def changed(old, new):
    """Polish description of what was (un)loaded between two scans, [] when the flagged set is the same."""
    if not old or not new or old.get("status") != "ok" or new.get("status") != "ok":
        return []
    a, b = {f["name"] for f in old["flagged"]}, {f["name"] for f in new["flagged"]}
    return [f"załadowano {n}" for n in sorted(b - a)] + [f"usunięto {n}" for n in sorted(a - b)]
