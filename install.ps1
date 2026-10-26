# PerfMonitor installer: collector as an elevated logon task + desktop/Start Menu shortcuts.
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
$user = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "1/3 Zadanie w harmonogramie (kolektor przy logowaniu, z uprawnieniami admina)..."
$action = New-ScheduledTaskAction -Execute $pyw -Argument '-m perfmon.collector' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -Priority 4 -StartWhenAvailable
Register-ScheduledTask -TaskName 'PerfMonitor Collector' -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Zbiera dane o wydajności, zawieszeniach i crashach (D:\PerfMonitor)' -Force | Out-Null
Start-ScheduledTask -TaskName 'PerfMonitor Collector'

Write-Host "2/3 Skróty PerfMonitor (pulpit + menu Start)..."
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir 'PerfMonitor.lnk'))
    $lnk.TargetPath = $pyw
    $lnk.Arguments = '-m perfmon.ui.app'
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$env:SystemRoot\System32\imageres.dll,144"
    $lnk.Description = 'PerfMonitor - wydajność i problemy'
    $lnk.Save()
}

Write-Host "3/3 Sprawdzanie..."
Start-Sleep -Seconds 6
$state = (Get-ScheduledTask -TaskName 'PerfMonitor Collector').State
Write-Host "Kolektor: $state"
Write-Host ""
Write-Host "Gotowe. Otwórz PerfMonitor ze skrótu na pulpicie." -ForegroundColor Green
Read-Host "Enter aby zamknąć"
