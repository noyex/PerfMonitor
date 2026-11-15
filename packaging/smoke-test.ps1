# Installs the freshly built setup the way the in-app updater does (silent), checks what it installed, runs the
# installed Python side for real, and uninstalls. For the release workflow's runner (elevated, nothing of NX Frame on
# it): do not run it on a PC that has NX Frame installed.
param([Parameter(Mandatory)][string]$Setup, [Parameter(Mandatory)][string]$Version)
$ErrorActionPreference = 'Stop'
function Check($ok, $what) { if ($ok) { Write-Host "ok: $what" } else { throw "FAILED: $what" } }
# Waits for the process itself: Start-Process -Wait would also wait for the window the installer leaves running
function Run($exe, $arguments) {
    $p = Start-Process $exe -ArgumentList $arguments -PassThru
    $null = $p.Handle   # keeps the exit code readable after the process is gone
    $p.WaitForExit()
    $p.ExitCode
}

$app = Join-Path $env:ProgramFiles 'NX Frame'
$task = 'NX Frame Collector'
$core = Join-Path $app 'nxframe-core.exe'
$log = Join-Path $env:TEMP 'nxframe-setup.log'

$code = Run $Setup "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=`"$log`""
if ($code -ne 0 -and (Test-Path $log)) { Get-Content $log -Tail 60 }
Check ($code -eq 0) "the installer finished (exit code $code)"
foreach ($file in 'NXFrame.exe', 'nxframe-core.exe', 'pm.exe', 'collector-task.ps1', '_internal\perfmon\agent\AGENT.md') {
    Check (Test-Path (Join-Path $app $file)) "$file is installed"
}

$action = (Get-ScheduledTask -TaskName $task).Actions[0]
Check ($action.Execute -eq $core -and $action.Arguments -eq '-m perfmon.collector') 'the logon task runs the installed collector'

& (Join-Path $app 'pm.exe') --help | Out-Null
Check ($LASTEXITCODE -eq 0) 'pm.exe starts'

# The API server the window starts, on a data folder of its own
$env:PERFMON_DATA_DIR = Join-Path $env:TEMP 'nxframe-smoke-data'
$port = 18765
$server = Start-Process $core -ArgumentList "-m perfmon.ui.server $port" -PassThru
try {
    $agent = $null
    foreach ($i in 1..30) {
        try { $agent = Invoke-RestMethod "http://127.0.0.1:$port/api/agent" -TimeoutSec 2; break } catch { Start-Sleep 1 }
    }
    Check ($agent -and $agent.cli -eq (Join-Path $app 'pm.exe')) 'the installed API server answers and names pm.exe'
    Check (Test-Path $agent.guide) 'the agent guide it names exists'
    $update = Invoke-RestMethod "http://127.0.0.1:$port/api/update" -TimeoutSec 30
    Check ($update.current -eq $Version) "the updater reports version $Version"
} finally {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}

# The collector, under its own mutex next to the one the task may have started. Informational: a CI machine has no
# sensors and may lack what the collector reads on a real PC.
$env:PERFMON_MUTEX = 'Global\NXFrameSmokeTest'
$collector = Start-Process $core -ArgumentList '-m perfmon.collector' -PassThru
$db = Join-Path $env:PERFMON_DATA_DIR 'perf.db'
foreach ($i in 1..40) { if ((Test-Path $db) -or $collector.HasExited) { break }; Start-Sleep 1 }
Start-Sleep 5
if ((Test-Path $db) -and -not $collector.HasExited) { Write-Host 'ok: the installed collector runs and writes its database' }
else { Write-Host "::warning::the installed collector did not keep running here (exited: $($collector.HasExited))" }
Stop-Process -Id $collector.Id -Force -ErrorAction SilentlyContinue
$collectorLog = Join-Path $env:PERFMON_DATA_DIR 'collector.log'
if (Test-Path $collectorLog) { Get-Content $collectorLog -Tail 25 }
Get-Process NXFrame, nxframe-core -ErrorAction SilentlyContinue | Format-Table Name, Id, Path -AutoSize | Out-String | Write-Host

$code = Run (Join-Path $app 'unins000.exe') '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
foreach ($i in 1..60) { if (-not (Test-Path (Join-Path $app 'NXFrame.exe'))) { break }; Start-Sleep 1 }
Check (-not (Test-Path (Join-Path $app 'NXFrame.exe'))) 'the uninstaller removed the program'
Check (-not (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue)) 'the uninstaller removed the logon task'
