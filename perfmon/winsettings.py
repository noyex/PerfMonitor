r"""Windows settings that matter for games, read without PowerShell (registry, WMI, ctypes). Read-only.

Unknown stays null (never a guess); where a whole reader cannot see the value it returns {"error": reason}.
Sources (verified on the Ryzen 7700X + RX 9070 XT, Windows 11 26200 PC):
- power: powrprof PowerGetActiveScheme / PowerReadFriendlyName (UTF-16: no "Zr?wnowa?ony" mojibake) /
  PowerReadACValueIndex (AC = mains, a desktop never runs on DC) for SUB_PROCESSOR PROCTHROTTLEMIN/MAX, CPMINCORES,
  PERFBOOSTMODE, PERFEPP; SUB_PCIEXPRESS ASPM; SUB_USB selective suspend. Same values as `powercfg /q`.
- monitor_state: QueryDisplayConfig (active paths: target refresh as a rational) + DisplayConfigGetDeviceInfo
  TARGET_NAME (friendly name) and ADVANCED_COLOR_INFO (HDR supported/enabled). Windows exposes no VRR state per
  monitor: vrr stays null here, FreeSync comes from ADL (amdsettings).
- directx: HKCU\Software\Microsoft\DirectX\UserGpuPreferences  DirectXUserGlobalSettings "k=v;" pairs
  (SwapEffectUpgradeEnable = optimizations for windowed games, VRROptimizeEnable, AutoHDREnable); absent = Windows
  default, kept as null. Per exe: a value named by the full exe path "GpuPreference=n;" (0 default, 1 power saving,
  2 high performance).
- compatibility flags: HKCU and HKLM ...\AppCompatFlags\Layers, value named by the exe path ("~ DISABLEDXMAXIMIZEDWINDOWEDMODE
  ..." = fullscreen optimizations disabled).
- timer_resolution: ntdll NtQueryTimerResolution (100 ns units; current = what running apps requested, volatile) and
  HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\kernel GlobalTimerResolutionRequests (absent here -> null).
- mmcss: HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile SystemResponsiveness,
  NetworkThrottlingIndex, Tasks\Games (GPU Priority, Priority, Scheduling Category, SFIO Priority).
- priority_separation: HKLM\SYSTEM\CurrentControlSet\Control\PriorityControl Win32PrioritySeparation.
- mouse: HKCU\Control Panel\Mouse MouseSpeed (1 with thresholds 6/10 = Enhance pointer precision on, 0 = off),
  MouseSensitivity. The collector runs elevated in the user's own session, so HKCU is the player's hive.
- defender: WMI root\Microsoft\Windows\Defender MSFT_MpComputerStatus RealTimeProtectionEnabled; MSFT_MpPreference
  ExclusionPath (non-admin gets "N/A: Must be an administrator..." -> games_excluded null).
- drivers: SetupAPI present devices of the System (AMD chipset), MEDIA, Net (physical only), Bluetooth and USB
  (controllers) classes -> their Class key DriverDesc / DriverVersion / DriverDate / ProviderName.
- kbs: WMI Win32_QuickFixEngineering HotFixID + InstalledOn.
"""
import ctypes
import os
import re
import uuid
from ctypes import wintypes

from . import winq
from .winq import HKCU, HKLM


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, s):
        return cls.from_buffer_copy(uuid.UUID(s).bytes_le)


# ---------------------------------------------------------------- power plan
SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"
POWER_SETTINGS = {   # key: (subgroup, setting)
    "cpu_min_pct": (SUB_PROCESSOR, "893dee8e-2bef-41e0-89c6-b55d0929964c"),
    "cpu_max_pct": (SUB_PROCESSOR, "bc5038f7-23e0-4960-96da-33abaf5935ec"),
    "core_parking_min_pct": (SUB_PROCESSOR, "0cc5b647-c1df-4637-891a-dec35c318583"),
    "boost_mode": (SUB_PROCESSOR, "be337238-0d82-4146-a960-4f3749d470c7"),
    "epp_pct": (SUB_PROCESSOR, "36687f9e-e3a5-4dbf-b1dc-15eb381c6863"),
    "pcie_aspm": ("501a4d13-42af-4429-9fd1-a8218c268e20", "ee12f906-d277-404b-b6da-e5fa1a576df5"),
    "usb_selective_suspend": ("2a737441-1930-4402-8d77-b2bebba308a3", "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"),
}
BOOST_MODES = {0: "disabled", 1: "enabled", 2: "aggressive", 3: "efficient_enabled", 4: "efficient_aggressive",
               5: "aggressive_at_guaranteed", 6: "efficient_aggressive_at_guaranteed"}
ASPM = {0: "off", 1: "moderate", 2: "maximum"}


def power():
    pp = ctypes.WinDLL("powrprof")
    active = ctypes.POINTER(GUID)()
    if pp.PowerGetActiveScheme(None, ctypes.byref(active)) != 0:
        return {"error": "PowerGetActiveScheme failed"}
    try:
        g = active.contents
        guid = str(uuid.UUID(bytes_le=bytes(ctypes.string_at(ctypes.addressof(g), 16))))
        size = wintypes.DWORD(0)
        pp.PowerReadFriendlyName(None, active, None, None, None, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value or 2)
        name = None
        if pp.PowerReadFriendlyName(None, active, None, None, buf, ctypes.byref(size)) == 0:
            name = buf.raw.decode("utf-16-le", errors="replace").split("\0", 1)[0] or None
        out = {"guid": guid, "name": name}
        for key, (sub, setting) in POWER_SETTINGS.items():
            v = wintypes.DWORD()
            ok = pp.PowerReadACValueIndex(None, active, ctypes.byref(GUID.of(sub)), ctypes.byref(GUID.of(setting)),
                                          ctypes.byref(v)) == 0
            out[key] = v.value if ok else None
        out["boost_mode"] = BOOST_MODES.get(out["boost_mode"], out["boost_mode"])
        out["pcie_aspm"] = ASPM.get(out["pcie_aspm"], out["pcie_aspm"])
        if out["usb_selective_suspend"] is not None:
            out["usb_selective_suspend"] = bool(out["usb_selective_suspend"])
        return out
    finally:
        ctypes.windll.kernel32.LocalFree(active)


# ---------------------------------------------------------------- monitors (DisplayConfig)
class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _PathSource(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class _PathTarget(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshNum", ctypes.c_uint32), ("refreshDen", ctypes.c_uint32), ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", wintypes.BOOL), ("statusFlags", ctypes.c_uint32)]


class _Path(ctypes.Structure):
    _fields_ = [("sourceInfo", _PathSource), ("targetInfo", _PathTarget), ("flags", ctypes.c_uint32)]


class _Header(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", _LUID), ("id", ctypes.c_uint32)]


class _TargetName(ctypes.Structure):
    _fields_ = [("header", _Header), ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32), ("monitorFriendlyDeviceName", ctypes.c_wchar * 64),
                ("monitorDevicePath", ctypes.c_wchar * 128)]


class _SourceName(ctypes.Structure):
    _fields_ = [("header", _Header), ("viewGdiDeviceName", ctypes.c_wchar * 32)]


class _AdvColor(ctypes.Structure):
    _fields_ = [("header", _Header), ("value", ctypes.c_uint32), ("colorEncoding", ctypes.c_uint32),
                ("bitsPerColorChannel", ctypes.c_uint32)]


def monitor_state():
    u32 = ctypes.windll.user32
    np_, nm = ctypes.c_uint32(), ctypes.c_uint32()
    if u32.GetDisplayConfigBufferSizes(2, ctypes.byref(np_), ctypes.byref(nm)) != 0:   # QDC_ONLY_ACTIVE_PATHS
        return []
    paths = (_Path * max(1, np_.value))()
    modes = ctypes.create_string_buffer(64 * max(1, nm.value))   # DISPLAYCONFIG_MODE_INFO is 64 bytes
    if u32.QueryDisplayConfig(2, ctypes.byref(np_), paths, ctypes.byref(nm), modes, None) != 0:
        return []
    out = []
    for p in paths[:np_.value]:
        t = p.targetInfo
        tn = _TargetName()
        tn.header = _Header(2, ctypes.sizeof(_TargetName), t.adapterId, t.id)
        name = path = None
        if u32.DisplayConfigGetDeviceInfo(ctypes.byref(tn)) == 0:
            name, path = tn.monitorFriendlyDeviceName or None, tn.monitorDevicePath
        sn = _SourceName()
        sn.header = _Header(1, ctypes.sizeof(_SourceName), p.sourceInfo.adapterId, p.sourceInfo.id)
        gdi = sn.viewGdiDeviceName if u32.DisplayConfigGetDeviceInfo(ctypes.byref(sn)) == 0 else None
        ac = _AdvColor()
        ac.header = _Header(9, ctypes.sizeof(_AdvColor), t.adapterId, t.id)
        hdr_sup = hdr_on = None
        if u32.DisplayConfigGetDeviceInfo(ctypes.byref(ac)) == 0:
            hdr_sup, hdr_on = bool(ac.value & 1), bool(ac.value & 2)
        m = re.search(r"DISPLAY#([^#]+)#", path or "", re.I)
        out.append({"name": name, "monitor": m.group(1) if m else None, "display": gdi,
                    "refresh_hz": round(t.refreshNum / t.refreshDen, 2) if t.refreshDen else None,
                    "hdr_supported": hdr_sup, "hdr": hdr_on, "vrr": None})
    return out


# ---------------------------------------------------------------- DirectX / per-exe
GPU_PREF = r"Software\Microsoft\DirectX\UserGpuPreferences"
LAYERS = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
GPU_PREF_NAMES = {"0": "default", "1": "power_saving", "2": "high_performance"}


def _kv(s):
    return dict(p.split("=", 1) for p in str(s or "").split(";") if "=" in p)


def directx():
    kv = _kv(winq.reg(HKCU, GPU_PREF, "DirectXUserGlobalSettings"))
    b = lambda k: None if k not in kv else kv[k] == "1"
    return {"vrr_optimization": b("VRROptimizeEnable"), "windowed_optimizations": b("SwapEffectUpgradeEnable"),
            "auto_hdr": b("AutoHDREnable"), "raw": kv or None}


def game_exes(paths):
    """[{path, gpu_preference, compat_flags, fso_disabled}] for the user's game exes (case-insensitive match)."""
    prefs = {k.lower(): v for k, v in (winq.reg_values(HKCU, GPU_PREF) or {}).items()}
    layers = {}
    for root in (HKLM, HKCU):   # HKCU last: per-user flags win, like Windows applies them
        layers.update({k.lower(): v for k, v in (winq.reg_values(root, LAYERS) or {}).items()})
    out = []
    for p in sorted({p.lower(): p for p in paths if p}.values()):
        pref = _kv(prefs.get(p.lower())).get("GpuPreference")
        flags = layers.get(p.lower())
        out.append({"path": p, "gpu_preference": GPU_PREF_NAMES.get(pref, pref) if pref is not None else "default",
                    "compat_flags": flags.strip() if flags else None,
                    "fso_disabled": "DISABLEDXMAXIMIZEDWINDOWEDMODE" in (flags or "").upper()})
    return out


# ---------------------------------------------------------------- scheduler / input
def timer_resolution():
    mx, mn, cur = wintypes.ULONG(), wintypes.ULONG(), wintypes.ULONG()
    ok = ctypes.WinDLL("ntdll").NtQueryTimerResolution(ctypes.byref(mx), ctypes.byref(mn), ctypes.byref(cur)) == 0
    return {"current_ms": cur.value / 1e4 if ok else None, "finest_ms": mn.value / 1e4 if ok else None,
            "global_requests": winq.reg(HKLM, r"SYSTEM\CurrentControlSet\Control\Session Manager\kernel",
                                        "GlobalTimerResolutionRequests")}


MMCSS = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"


def mmcss():
    games = winq.reg_values(HKLM, MMCSS + r"\Tasks\Games") or {}
    return {"system_responsiveness": winq.reg(HKLM, MMCSS, "SystemResponsiveness"),
            "network_throttling": winq.reg(HKLM, MMCSS, "NetworkThrottlingIndex"),
            "games_gpu_priority": games.get("GPU Priority"), "games_priority": games.get("Priority"),
            "games_scheduling": games.get("Scheduling Category"), "games_sfio": games.get("SFIO Priority")}


def mouse():
    v = winq.reg_values(HKCU, r"Control Panel\Mouse") or {}
    speed = v.get("MouseSpeed")
    return {"pointer_precision": None if speed is None else str(speed) != "0", "speed": v.get("MouseSensitivity"),
            "thresholds": [v.get("MouseThreshold1"), v.get("MouseThreshold2")]}


# ---------------------------------------------------------------- Defender
def _game_dirs(paths):
    """Library folder of each game: ...\\steamapps\\common\\<Game>, else the exe folder."""
    out = set()
    for p in paths or ():
        m = re.match(r"(.*?\\steamapps\\common\\[^\\]+)", p or "", re.I)
        out.add((m.group(1) if m else os.path.dirname(p or "")).rstrip("\\"))
    return sorted(d for d in out if d)


def defender(paths=()):
    ns = r"root\Microsoft\Windows\Defender"
    st = winq.wmi_safe("SELECT RealTimeProtectionEnabled, AntivirusEnabled, IsTamperProtected, AntivirusSignatureAge "
                       "FROM MSFT_MpComputerStatus", ns)
    out = {"realtime": None, "antivirus": None, "tamper_protection": None, "games_excluded": None}
    if st:
        s = st[0]
        out.update(realtime=s.get("RealTimeProtectionEnabled"), antivirus=s.get("AntivirusEnabled"),
                   tamper_protection=s.get("IsTamperProtected"), signature_age_days=s.get("AntivirusSignatureAge"))
    pref = winq.wmi_safe("SELECT ExclusionPath FROM MSFT_MpPreference", ns)
    excl = list((pref[0].get("ExclusionPath") if pref else None) or [])
    if any(str(e).startswith("N/A") for e in excl) or not pref:
        out["exclusions_unreadable"] = excl[0] if excl else "MSFT_MpPreference niedostępne"
        return out
    ex = [str(e).rstrip("\\").lower() for e in excl]
    out["exclusions"] = len(ex)
    out["games_excluded"] = [{"path": d, "excluded": any(d.lower() == e or d.lower().startswith(e + "\\") for e in ex)}
                             for d in _game_dirs(paths)]
    return out


# ---------------------------------------------------------------- drivers (SetupAPI present devices)
CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class"
DRIVER_CLASSES = {
    "chipset": "4d36e97d-e325-11ce-bfc1-08002be10318",     # System devices; only AMD ones are the chipset package
    "audio": "4d36e96c-e325-11ce-bfc1-08002be10318",
    "network": "4d36e972-e325-11ce-bfc1-08002be10318",
    "bluetooth": "e0cbf06c-cd8b-4647-bb8a-263b43f0f974",
    "usb": "36fc9e60-c465-11cf-8056-444553540000",
}


class _DevInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID), ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]


def _present_driver_keys(class_guid):
    """Class subkeys ('{guid}\\0003') of present devices of a setup class."""
    sa = ctypes.WinDLL("setupapi")
    sa.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    sa.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    sa.SetupDiGetDeviceRegistryPropertyW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                                     ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    sa.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    h = sa.SetupDiGetClassDevsW(ctypes.byref(GUID.of(class_guid)), None, None, 0x2)   # DIGCF_PRESENT
    if not h or h == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        i = 0
        while True:
            d = _DevInfo()
            d.cbSize = ctypes.sizeof(_DevInfo)
            if not sa.SetupDiEnumDeviceInfo(h, i, ctypes.byref(d)):
                break
            i += 1
            buf = ctypes.create_unicode_buffer(256)
            if sa.SetupDiGetDeviceRegistryPropertyW(h, ctypes.byref(d), 9, None, buf, ctypes.sizeof(buf), None):   # SPDRP_DRIVER
                out.append(buf.value)
    finally:
        sa.SetupDiDestroyDeviceInfoList(h)
    return out


def drivers():
    out = {}
    for kind, guid in DRIVER_CLASSES.items():
        items = {}
        for key in _present_driver_keys(guid):
            v = winq.reg_values(HKLM, CLASS_KEY + "\\" + key) or {}
            desc, prov = v.get("DriverDesc"), v.get("ProviderName") or ""
            if not desc:
                continue
            if kind == "chipset" and not re.search(r"\bAMD\b|Advanced Micro Devices", prov, re.I):
                continue
            if kind == "network" and not (v.get("Characteristics", 0) & 0x4):   # NCF_PHYSICAL: no virtual adapters
                continue
            if kind == "usb" and "controller" not in desc.lower():
                continue
            if kind in ("audio", "bluetooth") and prov.lower() == "microsoft":
                continue   # inbox class drivers (Bluetooth enumerators, generic audio endpoints)
            items.setdefault(desc, {"name": desc, "version": v.get("DriverVersion"),
                                    "date": winq.us_date(v.get("DriverDate")), "provider": prov or None})
        out[kind] = sorted(items.values(), key=lambda x: x["name"])
    return out


def kbs():
    rows = winq.wmi("SELECT HotFixID, InstalledOn FROM Win32_QuickFixEngineering")
    return sorted((f"{r['HotFixID']} ({winq.us_date(r.get('InstalledOn'))})" for r in rows if r.get("HotFixID")),
                  key=lambda s: s.split("(")[-1], reverse=True)


def priority_separation():
    return winq.reg(HKLM, r"SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation")
