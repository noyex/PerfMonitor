r"""Config snapshot: hardware, drivers, Windows settings important for games. Saved once a day.

All readers are read-only. Unknown stays null (never a guess). Registry values used (verified on the Ryzen 7700X +
RX 9070 XT + Adrenalin 26.10 PC):
- HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers  HwSchMode (HAGS; value absent here -> null)
- HKCU\Software\Microsoft\GameBar  AutoGameModeEnabled (absent = Windows default = on -> "default_on")
- HKCU\Software\AMD\DVR  InstantReplayEnabled, DvrEnabled, DesktopRecordingEnabled (Adrenalin recording)
- GPU class key {4d36e968-...}\000x (match DriverDesc to the adapter)  KMD_DeLagEnabled (Anti-Lag),
  KMD_RebarControlMode (driver mode only, not proof that ReBar is active)
- ReBar active = the dGPU's biggest memory BAR in Win32_DeviceMemoryAddress is above 256 MB (16 GB card + ReBar = 16 GB BAR)
"""
import json
import subprocess

from .diskinfo import DISK_MAP_PS, HEALTH_PS

PS = r"""
$ErrorActionPreference='SilentlyContinue'
function RegV($p,$n){ (Get-ItemProperty -Path $p -Name $n).$n }
$cs=Get-CimInstance Win32_ComputerSystem; $os=Get-CimInstance Win32_OperatingSystem
$dg=Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard
$plan=(powercfg /getactivescheme) -replace '.*:\s*',''
$cls='HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
$clsKeys=@(Get-ChildItem $cls | ? { $_.PSChildName -match '^\d{4}$' } | % { Get-ItemProperty $_.PSPath })
$pnpGpus=@(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='Display'")
function BarMb($pnp){ if(-not $pnp){return $null}; $d=$pnpGpus | ? { $_.PNPDeviceID -eq $pnp } | Select -First 1; if(-not $d){return $null}; $m=Get-CimAssociatedInstance -InputObject $d -ResultClassName Win32_DeviceMemoryAddress | % { [math]::Round(($_.EndingAddress-$_.StartingAddress+1)/1MB) } | Measure-Object -Maximum; if($m.Count){[int]$m.Maximum}else{$null} }
function ClsV($desc,$n){ ($clsKeys | ? { $_.DriverDesc -eq $desc } | Select -First 1).$n }
$dvr='HKCU:\Software\AMD\DVR'
[ordered]@{
 os = "$($os.Caption) $($os.Version) build $($os.BuildNumber).$((RegV 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' 'UBR'))"
 last_boot = $os.LastBootUpTime.ToString('s')
 cpu = (Get-CimInstance Win32_Processor).Name.Trim()
 board = (Get-CimInstance Win32_BaseBoard | % { "$($_.Manufacturer) $($_.Product)" })
 bios = (Get-CimInstance Win32_BIOS | % { "$($_.SMBIOSBIOSVersion) ($($_.ReleaseDate.ToString('yyyy-MM-dd')))" })
 ram = @(Get-CimInstance Win32_PhysicalMemory | % { "$($_.Manufacturer.Trim()) $($_.PartNumber.Trim()) $([math]::Round($_.Capacity/1GB))GB @ $($_.ConfiguredClockSpeed)MT/s (max $($_.Speed))" })
 ram_total_gb = [math]::Round($cs.TotalPhysicalMemory/1GB,1)
 gpus = @(Get-CimInstance Win32_VideoController | % { [ordered]@{name=$_.Name; driver=$_.DriverVersion; driver_date=$_.DriverDate.ToString('yyyy-MM-dd'); refresh=$_.CurrentRefreshRate; res="$($_.CurrentHorizontalResolution)x$($_.CurrentVerticalResolution)"; status=$_.Status; igpu=[bool]($_.Name -match 'Radeon\(TM\) Graphics|Vega|Intel|UHD|Iris'); bar_mb=(BarMb $_.PNPDeviceID); anti_lag=$(ClsV $_.Name 'KMD_DeLagEnabled'); rebar_mode=$(ClsV $_.Name 'KMD_RebarControlMode')} })
 disks = @(Get-PhysicalDisk | % { [ordered]@{name=$_.FriendlyName; media="$($_.MediaType)"; bus="$($_.BusType)"; size_gb=[math]::Round($_.Size/1GB); health="$($_.HealthStatus)"} })
 volumes = @(Get-Volume | ? DriveLetter | % { [ordered]@{letter="$($_.DriveLetter)"; free_gb=[math]::Round($_.SizeRemaining/1GB,1); size_gb=[math]::Round($_.Size/1GB,1)} })
 power_plan = $plan
 power_plan_guid = if("$plan" -match '[0-9a-f]{8}-[0-9a-f-]{27}'){$Matches[0]}else{$null}
 game_mode = $(if($null -eq (RegV 'HKCU:\Software\Microsoft\GameBar' 'AutoGameModeEnabled')){'default_on'}else{RegV 'HKCU:\Software\Microsoft\GameBar' 'AutoGameModeEnabled'})
 instant_replay = RegV $dvr 'InstantReplayEnabled'
 amd_recording = RegV $dvr 'DvrEnabled'
 amd_desktop_recording = RegV $dvr 'DesktopRecordingEnabled'
 game_dvr = RegV 'HKCU:\System\GameConfigStore' 'GameDVR_Enabled'
 hags = RegV 'HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers' 'HwSchMode'
 vbs_running = $dg.VirtualizationBasedSecurityStatus
 memory_integrity = RegV 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity' 'Enabled'
 pagefile = @(Get-CimInstance Win32_PageFileUsage | % { "$($_.Name) alloc=$($_.AllocatedBaseSize)MB peak=$($_.PeakUsage)MB" })
 pagefile_auto = $cs.AutomaticManagedPagefile
 fast_startup = RegV 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' 'HiberbootEnabled'
 startup_apps = @(Get-CimInstance Win32_StartupCommand | % { $_.Name })
 background_tools = @(Get-Process | ? { $_.Name -match 'EasyTune|GCC|RGBFusion|AorusEngine|Armoury|iCUE|NZXT|SignalRgb|OpenRGB|MSIAfterburner|RTSS|Overwolf|Discord|RadeonSoftware|AMDRSServ|Wallpaper|LGHUB|Razer|SteelSeries|Medal|OBS' } | Select -Expand Name -Unique)
 monitors = @(Get-CimInstance -Namespace root\wmi WmiMonitorID | % { ($_.UserFriendlyName | ? {$_ -ne 0} | % {[char]$_}) -join '' })
} | ConvertTo-Json -Depth 4 -Compress
"""
PS = PS.replace("\n} | ConvertTo-Json", f"\n {HEALTH_PS}\n {DISK_MAP_PS}\n}} | ConvertTo-Json")


PLAN_NAMES = {"381b4222-f694-41f0-9685-ff5bb260df2e": "Balanced", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "High performance",
              "a1841308-3541-4fab-bc81-f71556f20b4a": "Power saver", "e9a42b02-d5df-448d-aa00-03f14749eb61": "Ultimate performance"}


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
    info["power_plan_name"] = PLAN_NAMES.get((info.get("power_plan_guid") or "").lower())
    info["displays"] = displays() or None
    gpus = info.get("gpus") or []
    dgpus = [g for g in gpus if not g.get("igpu")]
    bars = [g["bar_mb"] for g in dgpus if g.get("bar_mb")]
    info["rebar"] = (max(bars) > 256) if bars else None   # BAR above 256 MB = Resizable BAR / SAM active
    return info


def collect() -> dict:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
                             capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
                             creationflags=0x08000000)
        return _post(json.loads(out.stdout.strip() or "{}"))
    except Exception as e:
        return {"error": str(e)}
