# NX Frame installer: collector as an elevated logon task + desktop/Start Menu shortcuts.
# Run: right click -> "Uruchom za pomocą programu PowerShell" (asks for admin).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pyw)) { throw "Brak środowiska Python: $pyw" }
$exe = Join-Path $root 'NXFrame.exe'   # the window from build-ui.ps1; without it the shortcuts open the Python window
$user = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "1/3 Zadanie w harmonogramie (kolektor przy logowaniu, z uprawnieniami admina)..."
# The task was called 'PerfMonitor Collector' before the rename: remove it so two collectors never run.
# The collector mutex keeps its old name, so a still running old collector blocks the new one until it is stopped here.
Stop-ScheduledTask -TaskName 'PerfMonitor Collector' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'PerfMonitor Collector' -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -match 'perfmon\.collector' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$action = New-ScheduledTaskAction -Execute $pyw -Argument '-m perfmon.collector' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -Priority 4 -StartWhenAvailable
Register-ScheduledTask -TaskName 'NX Frame Collector' -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Zbiera dane o wydajności, zawieszeniach i crashach ($root)" -Force | Out-Null
Start-ScheduledTask -TaskName 'NX Frame Collector'

Write-Host "2/3 Skróty NX Frame (pulpit + menu Start)..."
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
    Remove-Item (Join-Path $dir 'PerfMonitor.lnk') -ErrorAction SilentlyContinue
    $lnk = $shell.CreateShortcut((Join-Path $dir 'NX Frame.lnk'))
    if (Test-Path $exe) {
        $lnk.TargetPath = $exe
        $lnk.Arguments = ''
        $lnk.IconLocation = "$exe,0"
    } else {
        $lnk.TargetPath = $pyw
        $lnk.Arguments = '-m perfmon.ui.app'
        $lnk.IconLocation = "$root\perfmon\ui\static\favicon.ico"
    }
    $lnk.WorkingDirectory = $root
    $lnk.Description = 'NX Frame - wydajność i problemy'
    $lnk.Save()
}

Write-Host "3/3 Sprawdzanie..."
Start-Sleep -Seconds 6
$state = (Get-ScheduledTask -TaskName 'NX Frame Collector').State
Write-Host "Kolektor: $state"
Write-Host ""
Write-Host "Gotowe. Otwórz NX Frame ze skrótu na pulpicie." -ForegroundColor Green
Read-Host "Enter aby zamknąć"
