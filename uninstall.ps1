# Removes the logon task and shortcuts. Data in D:\PerfMonitor\data stays.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}
Stop-ScheduledTask -TaskName 'PerfMonitor Collector' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'PerfMonitor Collector' -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -match 'perfmon\.collector' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
    Remove-Item (Join-Path $dir 'PerfMonitor.lnk') -ErrorAction SilentlyContinue
}
Write-Host "Usunięto. Dane zostały w D:\PerfMonitor\data."
Read-Host "Enter aby zamknąć"
