# Builds the PerfMonitor window (src-tauri, Rust) and puts it at <root>\PerfMonitor.exe, where install.ps1 looks for it.
# Run: powershell -ExecutionPolicy Bypass -File build-ui.ps1
# Needed again only after changes in src-tauri: the window reads perfmon\ui\static from disk at runtime.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$cargo = (Get-Command cargo -ErrorAction SilentlyContinue).Source
if (-not $cargo -and (Test-Path "$env:USERPROFILE\.cargo\bin\cargo.exe")) { $cargo = "$env:USERPROFILE\.cargo\bin\cargo.exe" }
if (-not $cargo) {
    Write-Host "Brak Rusta (cargo). Zainstaluj go: winget install Rustlang.Rustup" -ForegroundColor Red
    exit 1
}
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$msvc = if (Test-Path $vswhere) { & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath }
if (-not $msvc) {
    Write-Host "Brak kompilatora C++ (MSVC). Zainstaluj Visual Studio 2022 Build Tools z obciążeniem C++:" -ForegroundColor Red
    Write-Host "winget install Microsoft.VisualStudio.2022.BuildTools --override `"--passive --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended`""
    exit 1
}

Write-Host "Budowanie okna PerfMonitor (pierwszy raz trwa kilka minut)..."
Push-Location (Join-Path $root 'src-tauri')
try {
    & $cargo build --release --features custom-protocol
    if ($LASTEXITCODE -ne 0) { throw "Budowanie nie powiodło się (cargo, kod $LASTEXITCODE)." }
} finally {
    Pop-Location
}

$exe = Join-Path $root 'PerfMonitor.exe'
try {
    Copy-Item (Join-Path $root 'src-tauri\target\release\perfmonitor.exe') $exe -Force
} catch {
    throw "Nie można zastąpić $exe. Zamknij okno PerfMonitor i uruchom skrypt ponownie."
}
Write-Host "Gotowe: $exe" -ForegroundColor Green
Write-Host "Uruchom install.ps1, żeby skróty na pulpicie i w menu Start otwierały PerfMonitor.exe."
