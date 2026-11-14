# Registers (or with -Remove unregisters) the collector logon task of the installed build. Run elevated:
# the installer calls it after copying files and from the uninstaller.
param([Parameter(Mandatory)][string]$AppDir, [switch]$Remove)
$ErrorActionPreference = 'Stop'
$name = 'PerfMonitor Collector'
$exe = Join-Path $AppDir 'PerfMonitor.exe'

Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    exit 0
}
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $exe -Argument '-m perfmon.collector' -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -Priority 4 -StartWhenAvailable
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'PerfMonitor: collects performance, freeze and crash data' -Force | Out-Null
Start-ScheduledTask -TaskName $name
