"""Context recorded at each game session start: driver, OS, BIOS and game build — enables before/after comparisons."""
import glob
import os
import re
import winreg

import win32api

GPU_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def _reg(root, path, name):
    try:
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def gpu_drivers():
    """All display adapters from the driver registry: desc, driver version/date, Adrenalin version."""
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, GPU_CLASS) as cls:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(cls, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                p = GPU_CLASS + "\\" + sub
                desc = _reg(winreg.HKEY_LOCAL_MACHINE, p, "DriverDesc")
                if not desc:
                    continue
                out.append({
                    "name": desc,
                    "driver": _reg(winreg.HKEY_LOCAL_MACHINE, p, "DriverVersion"),
                    "date": _reg(winreg.HKEY_LOCAL_MACHINE, p, "DriverDate"),
                    "adrenalin": _reg(winreg.HKEY_LOCAL_MACHINE, p, "RadeonSoftwareVersion"),
                })
    except OSError:
        pass
    return out


def main_gpu(gpus, preferred_name=None):
    if preferred_name:
        for g in gpus:
            if g["name"] == preferred_name:
                return g
    # Fallback: skip the Ryzen iGPU ("AMD Radeon(TM) Graphics")
    for g in gpus:
        if "(TM) Graphics" not in g["name"]:
            return g
    return gpus[0] if gpus else {}


def exe_version(path):
    try:
        info = win32api.GetFileVersionInfo(path, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


def steam_info(path):
    """Steam build id and last update time from steamapps/appmanifest_*.acf."""
    pl = (path or "").lower()
    i = pl.find("\\steamapps\\common\\")
    if i < 0:
        return None
    steamapps = path[: i + len("\\steamapps")]
    installdir = path[i + len("\\steamapps\\common\\"):].split("\\")[0].lower()
    for acf in glob.glob(os.path.join(steamapps, "appmanifest_*.acf")):
        try:
            txt = open(acf, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        get = lambda k: (re.search(rf'"{k}"\s+"([^"]*)"', txt, re.I) or [None, None])[1]
        if (get("installdir") or "").lower() == installdir:
            lu = get("LastUpdated")
            return {"appid": get("appid"), "name": get("name"), "buildid": get("buildid"),
                    "last_updated": float(lu) if lu and lu.isdigit() else None}
    return None


def collect(exe_path, gpu_name=None):
    gpu = main_gpu(gpu_drivers(), gpu_name)
    cv = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    build = _reg(winreg.HKEY_LOCAL_MACHINE, cv, "CurrentBuild")
    ubr = _reg(winreg.HKEY_LOCAL_MACHINE, cv, "UBR")
    try:
        mtime = os.path.getmtime(exe_path) if exe_path else None
    except OSError:
        mtime = None
    return {
        "gpu": gpu.get("name"),
        "gpu_driver": gpu.get("driver"),
        "gpu_driver_date": gpu.get("date"),
        "adrenalin": gpu.get("adrenalin"),
        "os_build": f"{build}.{ubr}" if build else None,
        "bios": _reg(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS", "BIOSVersion"),
        "game_version": exe_version(exe_path) if exe_path else None,
        "exe_mtime": mtime,
        "steam": steam_info(exe_path),
    }
