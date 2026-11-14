r"""Config snapshot: hardware, drivers, Windows settings important for games. Taken daily (full, and at collector
start) and at every game session start (quick: without disk health, drivers/KBs and display state).

All readers are read-only. Unknown stays null (never a guess). Snapshot parts are providers (register()): each
returns a dict merged into the snapshot; one failing provider only adds snapshot.errors[name].
Registry values used (verified on the Ryzen 7700X + RX 9070 XT + Adrenalin 26.10 PC):
- HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers  HwSchMode (HAGS; value absent here -> null)
- HKCU\Software\Microsoft\GameBar  AutoGameModeEnabled (absent = Windows default = on -> "default_on")
- HKCU\Software\AMD\DVR  InstantReplayEnabled, DvrEnabled, DesktopRecordingEnabled (Adrenalin recording)
- GPU class key {4d36e968-...}\000x (match DriverDesc to the adapter)  KMD_DeLagEnabled (Anti-Lag),
  KMD_RebarControlMode (driver mode only, not proof that ReBar is active)
- ReBar active = the dGPU's biggest memory BAR in Win32_DeviceMemoryAddress is above 256 MB (16 GB card + ReBar = 16 GB BAR)
WMI (in-process COM, winq): Win32_OperatingSystem/Processor/BaseBoard/BIOS/PhysicalMemory/VideoController/
PageFileUsage/ComputerSystem/StartupCommand, root\wmi WmiMonitorID, root\Microsoft\Windows\Storage MSFT_PhysicalDisk
and MSFT_Partition, root\Microsoft\Windows\DeviceGuard Win32_DeviceGuard. Field formats match the PowerShell era so
old and new snapshots diff cleanly. PowerShell remains only for Get-StorageReliabilityCounter (disk_health, full
snapshot only): its WMI association could not be verified here without admin rights.
New sections: power, monitor_state, directx, game_exes, timer_resolution, mmcss, priority_separation, mouse,
defender, drivers, kbs (perfmon/winsettings.py) and amd (perfmon/amdsettings.py).
"""
import json
import re
import subprocess
import time

import psutil

from . import amdsettings, dumpconfig, winq, winsettings
from .diskinfo import HEALTH_PS
from .winq import HKCU, HKLM

PLAN_NAMES = {"381b4222-f694-41f0-9685-ff5bb260df2e": "Balanced", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "High performance",
              "a1841308-3541-4fab-bc81-f71556f20b4a": "Power saver", "e9a42b02-d5df-448d-aa00-03f14749eb61": "Ultimate performance"}
GPU_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
IGPU_RE = re.compile(r"Radeon\(TM\) Graphics|Vega|Intel|UHD|Iris")
TOOLS_RE = re.compile(r"EasyTune|GCC|RGBFusion|AorusEngine|Armoury|iCUE|NZXT|SignalRgb|OpenRGB|MSIAfterburner|RTSS|"
                      r"Overwolf|Discord|RadeonSoftware|AMDRSServ|Wallpaper|LGHUB|Razer|SteelSeries|Medal|OBS", re.I)
MEDIA = {0: "Unspecified", 3: "HDD", 4: "SSD", 5: "SCM"}
BUS = {0: "Unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "Fibre Channel", 7: "USB", 8: "RAID",
       9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC", 14: "Virtual", 15: "File Backed Virtual",
       16: "Storage Spaces", 17: "NVMe", 18: "SCM", 19: "UFS"}
HEALTH = {0: "Healthy", 1: "Warning", 2: "Unhealthy", 5: "Unknown"}

PROVIDERS = []   # [(name, fn(ctx) -> dict, quick)]


def register(name, fn, quick=True):
    """Add a snapshot provider. fn(ctx) gets {"quick": bool, "game_paths": [...], "gpu_name": str|None} and returns
    keys to merge into the snapshot. quick=False: daily snapshot only (slow or heavy readers)."""
    global PROVIDERS
    PROVIDERS = [p for p in PROVIDERS if p[0] != name] + [(name, fn, quick)]


def provider(name, quick=True):
    def deco(fn):
        register(name, fn, quick)
        return fn
    return deco


def _gb(v, nd=0):
    try:
        return round(float(v) / 2**30, nd) if nd else round(float(v) / 2**30)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- legacy keys (PowerShell era formats)
@provider("system")
def _system(ctx):
    os_ = (winq.wmi("SELECT Caption, Version, BuildNumber FROM Win32_OperatingSystem") or [{}])[0]
    ubr = winq.reg(HKLM, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "UBR")
    bios = (winq.wmi("SELECT SMBIOSBIOSVersion, ReleaseDate FROM Win32_BIOS") or [{}])[0]
    board = (winq.wmi("SELECT Manufacturer, Product FROM Win32_BaseBoard") or [{}])[0]
    cpu = (winq.wmi("SELECT Name FROM Win32_Processor") or [{}])[0]
    ram = [f"{(m.get('Manufacturer') or '').strip()} {(m.get('PartNumber') or '').strip()} {_gb(m.get('Capacity'))}GB @ "
           f"{m.get('ConfiguredClockSpeed')}MT/s (max {m.get('Speed')})"
           for m in winq.wmi("SELECT Manufacturer, PartNumber, Capacity, ConfiguredClockSpeed, Speed FROM Win32_PhysicalMemory")]
    return {
        "os": f"{os_.get('Caption')} {os_.get('Version')} build {os_.get('BuildNumber')}.{ubr if ubr is not None else ''}",
        "last_boot": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(psutil.boot_time())),
        "cpu": (cpu.get("Name") or "").strip() or None,
        "board": f"{board.get('Manufacturer')} {board.get('Product')}" if board else None,
        "bios": f"{bios.get('SMBIOSBIOSVersion')} ({winq.wmi_date(bios.get('ReleaseDate'))})" if bios else None,
        "ram": ram,
        "ram_total_gb": round(psutil.virtual_memory().total / 2**30, 1),
    }


def _bar_mb(pnp):
    """Biggest memory BAR of a PCI device (MB) from Win32_DeviceMemoryAddress."""
    if not pnp:
        return None
    rows = winq.wmi_safe('ASSOCIATORS OF {Win32_PnPEntity.DeviceID="%s"} WHERE ResultClass=Win32_DeviceMemoryAddress'
                         % pnp.replace("\\", "\\\\").replace('"', '\\"'))   # single quotes + escapes: 0x80041002
    sizes = []
    for r in rows:
        try:
            sizes.append(round((int(r["EndingAddress"]) - int(r["StartingAddress"]) + 1) / 2**20))
        except (KeyError, TypeError, ValueError):
            pass
    return max(sizes) if sizes else None


def _class_values(desc):
    for sub in winq.reg_subkeys(HKLM, GPU_CLASS):
        if sub.isdigit():
            v = winq.reg_values(HKLM, GPU_CLASS + "\\" + sub) or {}
            if v.get("DriverDesc") == desc:
                return v
    return {}


@provider("gpus")
def _gpus(ctx):
    out = []
    for g in winq.wmi("SELECT Name, DriverVersion, DriverDate, CurrentRefreshRate, CurrentHorizontalResolution, "
                      "CurrentVerticalResolution, Status, PNPDeviceID FROM Win32_VideoController"):
        cls = _class_values(g.get("Name"))
        out.append({"name": g.get("Name"), "driver": g.get("DriverVersion"), "driver_date": winq.wmi_date(g.get("DriverDate")),
                    "refresh": g.get("CurrentRefreshRate"),
                    "res": f"{g.get('CurrentHorizontalResolution') or ''}x{g.get('CurrentVerticalResolution') or ''}",
                    "status": g.get("Status"), "igpu": bool(IGPU_RE.search(g.get("Name") or "")),
                    # ASSOCIATORS takes 0.5-7 s and the BAR only changes with BIOS settings: daily snapshot only
                    "bar_mb": None if ctx.get("quick") else _bar_mb(g.get("PNPDeviceID")),
                    "anti_lag": cls.get("KMD_DeLagEnabled"),
                    "rebar_mode": cls.get("KMD_RebarControlMode")})
    return {"gpus": out}


@provider("storage")
def _storage(ctx):
    ns = r"root\Microsoft\Windows\Storage"
    disks = [{"name": d.get("FriendlyName"), "media": MEDIA.get(d.get("MediaType"), str(d.get("MediaType"))),
              "bus": BUS.get(d.get("BusType"), str(d.get("BusType"))), "size_gb": _gb(d.get("Size")),
              "health": HEALTH.get(d.get("HealthStatus"), str(d.get("HealthStatus")))}
             for d in winq.wmi("SELECT FriendlyName, MediaType, BusType, Size, HealthStatus FROM MSFT_PhysicalDisk", ns)]
    dmap = [{"letter": chr(p["DriveLetter"]), "disk": p.get("DiskNumber")}
            for p in winq.wmi("SELECT DriveLetter, DiskNumber FROM MSFT_Partition", ns) if p.get("DriveLetter")]
    vols = []
    for p in psutil.disk_partitions(all=False):
        if "fixed" not in (p.opts or "") and "removable" not in (p.opts or ""):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        vols.append({"letter": p.mountpoint[:1], "free_gb": round(u.free / 2**30, 1), "size_gb": round(u.total / 2**30, 1)})
    return {"disks": disks, "volumes": vols, "disk_map": dmap}


DISK_HEALTH_PS = "$ErrorActionPreference='SilentlyContinue'\n[ordered]@{\n " + HEALTH_PS + "\n} | ConvertTo-Json -Depth 4 -Compress"


@provider("disk_health", quick=False)
def _disk_health(ctx):
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", DISK_HEALTH_PS],
                         capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
                         creationflags=0x08000000)
    return {"disk_health": json.loads(out.stdout.strip() or "{}").get("disk_health")}


@provider("windows")
def _windows(ctx):
    dvr = r"Software\AMD\DVR"
    gm = winq.reg(HKCU, r"Software\Microsoft\GameBar", "AutoGameModeEnabled")
    dg = winq.wmi_safe("SELECT VirtualizationBasedSecurityStatus FROM Win32_DeviceGuard", r"root\Microsoft\Windows\DeviceGuard")
    cs = (winq.wmi_safe("SELECT AutomaticManagedPagefile FROM Win32_ComputerSystem") or [{}])[0]
    pw = winsettings.power()
    guid = pw.get("guid")
    return {
        "power_plan": f"{guid}  ({pw.get('name')})" if guid else None,
        "power_plan_guid": guid,
        "power_plan_name": PLAN_NAMES.get((guid or "").lower()),
        "power": pw,
        "game_mode": "default_on" if gm is None else gm,
        "instant_replay": winq.reg(HKCU, dvr, "InstantReplayEnabled"),
        "amd_recording": winq.reg(HKCU, dvr, "DvrEnabled"),
        "amd_desktop_recording": winq.reg(HKCU, dvr, "DesktopRecordingEnabled"),
        "game_dvr": winq.reg(HKCU, r"System\GameConfigStore", "GameDVR_Enabled"),
        "hags": winq.reg(HKLM, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode"),
        "vbs_running": dg[0].get("VirtualizationBasedSecurityStatus") if dg else None,
        "memory_integrity": winq.reg(HKLM, r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity", "Enabled"),
        "pagefile": [f"{p.get('Name')} alloc={p.get('AllocatedBaseSize')}MB peak={p.get('PeakUsage')}MB"
                     for p in winq.wmi_safe("SELECT Name, AllocatedBaseSize, PeakUsage FROM Win32_PageFileUsage")],
        "pagefile_auto": cs.get("AutomaticManagedPagefile"),
        "fast_startup": winq.reg(HKLM, r"SYSTEM\CurrentControlSet\Control\Session Manager\Power", "HiberbootEnabled"),
        "startup_apps": [s["Name"] for s in winq.wmi_safe("SELECT Name FROM Win32_StartupCommand") if s.get("Name")],
        "background_tools": sorted({(p.info["name"] or "").rsplit(".", 1)[0] for p in psutil.process_iter(["name"])
                                    if TOOLS_RE.search(p.info["name"] or "")}),
        "monitors": ["".join(chr(c) for c in (m.get("UserFriendlyName") or ()) if c)
                     for m in winq.wmi_safe("SELECT UserFriendlyName FROM WmiMonitorID", r"root\wmi")],
    }


# ---------------------------------------------------------------- new sections
@provider("display", quick=False)   # a starting game may switch modes: the desktop state is the setting
def _display(ctx):
    return {"displays": displays() or None, "monitor_state": winsettings.monitor_state() or None,
            "directx": winsettings.directx()}


@provider("tuning")
def _tuning(ctx):
    return {"game_exes": winsettings.game_exes(ctx.get("game_paths") or []),
            "timer_resolution": winsettings.timer_resolution(), "mmcss": winsettings.mmcss(),
            "priority_separation": winsettings.priority_separation(), "mouse": winsettings.mouse(),
            "defender": winsettings.defender(ctx.get("game_paths") or [])}


@provider("drivers", quick=False)   # SetupAPI + QFE ~0.7 s; drivers change with reboots (daily runs at start)
def _drivers(ctx):
    return {"drivers": winsettings.drivers(), "kbs": winsettings.kbs()}


@provider("amd")
def _amd(ctx):
    return {"amd": amdsettings.collect(ctx.get("gpu_name"))}


@provider("crash_dump")
def _crash_dump(ctx):
    try:
        return {"crash_dump": dumpconfig.read()}
    except Exception:
        return {"crash_dump": None}   # unreadable = null, never a config change


def displays() -> list:
    """Which adapter drives which monitor (EnumDisplayDevices; Win32_VideoController shows stale modes for an idle iGPU)."""
    try:
        import win32api
        from . import freezecheck
        out = []
        for d in freezecheck.displays():
            try:
                mon = win32api.EnumDisplayDevices(d["display"], 0).DeviceID.split("\\")[1]   # MONITOR\GBT274E\... -> GBT274E
            except Exception:
                mon = None
            out.append({"adapter": d["adapter"], "monitor": mon, "mode": d["mode"], "primary": d["primary"]})
        return out
    except Exception:
        return []


def _post(info: dict) -> dict:
    gpus = info.get("gpus") or []
    dgpus = [g for g in gpus if not g.get("igpu")]
    bars = [g["bar_mb"] for g in dgpus if g.get("bar_mb")]
    if "gpus" in info:
        info["rebar"] = (max(bars) > 256) if bars else None   # BAR above 256 MB = Resizable BAR / SAM active
    return info


def collect(quick=False, game_paths=(), gpu_name=None) -> dict:
    """Snapshot dict; snapshot = {quick, seconds, errors, providers: {name: seconds}}."""
    ctx = {"quick": quick, "game_paths": list(game_paths or []), "gpu_name": gpu_name}
    info, errors, timing = {}, {}, {}
    t0 = time.perf_counter()
    for name, fn, q in list(PROVIDERS):
        if quick and not q:
            continue
        t = time.perf_counter()
        try:
            info.update(fn(ctx) or {})
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"
        timing[name] = round(time.perf_counter() - t, 3)
    info = _post(info)
    info["snapshot"] = {"quick": quick, "seconds": round(time.perf_counter() - t0, 2), "providers": timing,
                        "errors": errors or None}
    return info
