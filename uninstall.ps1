# Removes the logon task and shortcuts (the NX Frame names and the old PerfMonitor ones). Data in <root>\data stays.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}
foreach ($task in @('NX Frame Collector', 'PerfMonitor Collector')) {
    Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -match 'perfmon\.collector' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
    foreach ($name in @('NX Frame.lnk', 'PerfMonitor.lnk')) {
        Remove-Item (Join-Path $dir $name) -ErrorAction SilentlyContinue
    }
}
Write-Host "Usunięto. Dane zostały w $root\data."
Read-Host "Enter aby zamknąć"
