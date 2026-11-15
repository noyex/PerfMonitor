# The collector logon task and the running processes of the installed build. The installer runs it elevated:
#   -Stop     before files are replaced: stop the task and every running exe of the program, wait until they are gone
#   (nothing) after files are copied: register the task and start the collector
#   -Remove   from the uninstaller: -Stop, then unregister the task
param([Parameter(Mandatory)][string]$AppDir, [switch]$Stop, [switch]$Remove)
$ErrorActionPreference = 'Stop'
$name = 'NX Frame Collector'   # the same task install.ps1 registers for a source checkout: one collector per PC
$core = Join-Path $AppDir 'nxframe-core.exe'

if ($Stop -or $Remove) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    # by path, not by name alone: another program's pm.exe is not ours to stop
    $running = @(Get-Process -Name NXFrame, nxframe-core, pm -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith("$AppDir\", [StringComparison]::OrdinalIgnoreCase) })
    $running | Stop-Process -Force -ErrorAction SilentlyContinue
    $running | Wait-Process -Timeout 15 -ErrorAction SilentlyContinue
    if ($Remove) { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue }
    exit 0
}

# The user at the keyboard, not the account that answered the UAC prompt: they differ when a standard user installs
# with an administrator's password, and the task must start at the logon of the one who uses the PC
$user = (Get-CimInstance Win32_ComputerSystem).UserName
if (-not $user) { $user = "$env:USERDOMAIN\$env:USERNAME" }
$action = New-ScheduledTaskAction -Execute $core -Argument '-m perfmon.collector' -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -Priority 4 -StartWhenAvailable
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'NX Frame: collects performance, freeze and crash data' -Force | Out-Null
Start-ScheduledTask -TaskName $name
