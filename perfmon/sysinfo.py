"""Config snapshot: hardware, drivers, Windows settings important for games. Saved once a day."""
import json
import subprocess

PS = r"""
$ErrorActionPreference='SilentlyContinue'
function RegV($p,$n){ (Get-ItemProperty -Path $p -Name $n).$n }
$cs=Get-CimInstance Win32_ComputerSystem; $os=Get-CimInstance Win32_OperatingSystem
$dg=Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard
$plan=(powercfg /getactivescheme) -replace '.*:\s*',''
[ordered]@{
 os = "$($os.Caption) $($os.Version) build $($os.BuildNumber).$((RegV 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' 'UBR'))"
 last_boot = $os.LastBootUpTime.ToString('s')
 cpu = (Get-CimInstance Win32_Processor).Name.Trim()
 board = (Get-CimInstance Win32_BaseBoard | % { "$($_.Manufacturer) $($_.Product)" })
 bios = (Get-CimInstance Win32_BIOS | % { "$($_.SMBIOSBIOSVersion) ($($_.ReleaseDate.ToString('yyyy-MM-dd')))" })
 ram = @(Get-CimInstance Win32_PhysicalMemory | % { "$($_.Manufacturer.Trim()) $($_.PartNumber.Trim()) $([math]::Round($_.Capacity/1GB))GB @ $($_.ConfiguredClockSpeed)MT/s (max $($_.Speed))" })
 ram_total_gb = [math]::Round($cs.TotalPhysicalMemory/1GB,1)
 gpus = @(Get-CimInstance Win32_VideoController | % { [ordered]@{name=$_.Name; driver=$_.DriverVersion; driver_date=$_.DriverDate.ToString('yyyy-MM-dd'); refresh=$_.CurrentRefreshRate; res="$($_.CurrentHorizontalResolution)x$($_.CurrentVerticalResolution)"; status=$_.Status} })
 disks = @(Get-PhysicalDisk | % { [ordered]@{name=$_.FriendlyName; media="$($_.MediaType)"; bus="$($_.BusType)"; size_gb=[math]::Round($_.Size/1GB); health="$($_.HealthStatus)"} })
 volumes = @(Get-Volume | ? DriveLetter | % { [ordered]@{letter="$($_.DriveLetter)"; free_gb=[math]::Round($_.SizeRemaining/1GB,1); size_gb=[math]::Round($_.Size/1GB,1)} })
 power_plan = $plan
 game_mode = RegV 'HKCU:\Software\Microsoft\GameBar' 'AutoGameModeEnabled'
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


def collect() -> dict:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
                             capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
                             creationflags=0x08000000)
        return json.loads(out.stdout.strip() or "{}")
    except Exception as e:
        return {"error": str(e)}
