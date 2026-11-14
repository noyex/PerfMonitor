; Inno Setup script of the installer. Build (after PyInstaller):
;   iscc /DAppVersion=0.1.0 packaging\PerfMonitor.iss   ->  dist\PerfMonitor-Setup-0.1.0.exe
; The in-app updater (perfmon/updater.py) runs it with /SILENT: the same steps without the wizard pages.
#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z
#endif

[Setup]
AppId={{6F1C3E52-8B0A-4D7E-9C55-2A7B1E4D9F31}
AppName=PerfMonitor
AppVersion={#AppVersion}
AppPublisher=noyex
AppPublisherURL=https://github.com/noyex/PerfMonitor
AppUpdatesURL=https://github.com/noyex/PerfMonitor/releases
DefaultDirName={autopf}\PerfMonitor
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=PerfMonitor-Setup-{#AppVersion}
SetupIconFile=..\perfmon\ui\static\favicon.ico
UninstallDisplayIcon={app}\PerfMonitor.exe
UninstallDisplayName=PerfMonitor
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; the collector and the UI are stopped in PrepareToInstall, not through Restart Manager
CloseApplications=no

[Languages]
Name: "pl"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
pl.DesktopIcon=Skrót na pulpicie
en.DesktopIcon=Desktop shortcut
pl.Sensors=Zainstaluj LibreHardwareMonitor i PresentMon (winget): temperatury, zegary i klatki w grach
en.Sensors=Install LibreHardwareMonitor and PresentMon (winget): temperatures, clocks and game frames
pl.Launch=Uruchom PerfMonitor
en.Launch=Launch PerfMonitor
pl.Collector=Uruchamianie kolektora...
en.Collector=Starting the collector...
pl.Winget=Instalowanie LibreHardwareMonitor i PresentMon...
en.Winget=Installing LibreHardwareMonitor and PresentMon...

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"
Name: "sensors"; Description: "{cm:Sensors}"; Check: not WizardSilent

[Files]
Source: "..\dist\PerfMonitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "collector-task.ps1"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; a previous version's runtime: PyInstaller file names change between Python / package versions
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\PerfMonitor"; Filename: "{app}\PerfMonitor.exe"
Name: "{autodesktop}\PerfMonitor"; Filename: "{app}\PerfMonitor.exe"; Tasks: desktopicon

[Run]
Filename: "winget.exe"; Parameters: "install --exact --id LibreHardwareMonitor.LibreHardwareMonitor --silent --accept-package-agreements --accept-source-agreements --disable-interactivity"; StatusMsg: "{cm:Winget}"; Tasks: sensors; Flags: runhidden skipifdoesntexist
Filename: "winget.exe"; Parameters: "install --exact --id Intel.PresentMon.Console --silent --accept-package-agreements --accept-source-agreements --disable-interactivity"; StatusMsg: "{cm:Winget}"; Tasks: sensors; Flags: runhidden skipifdoesntexist
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\collector-task.ps1"" -AppDir ""{app}"""; StatusMsg: "{cm:Collector}"; Flags: runhidden
; after an update (silent) the UI comes back on its own; after a wizard install it is a checkbox
Filename: "{app}\PerfMonitor.exe"; Description: "{cm:Launch}"; Flags: nowait postinstall runasoriginaluser skipifsilent
Filename: "{app}\PerfMonitor.exe"; Flags: nowait runasoriginaluser; Check: WizardSilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\collector-task.ps1"" -AppDir ""{app}"" -Remove"; Flags: runhidden; RunOnceId: "RemoveTask"
Filename: "taskkill.exe"; Parameters: "/F /IM PerfMonitor.exe /IM pm.exe"; Flags: runhidden; RunOnceId: "KillApp"

[Code]
// Stops the collector task and every PerfMonitor.exe / pm.exe before files are replaced (they hold _internal open).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "PerfMonitor Collector"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM PerfMonitor.exe /IM pm.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(1000);
  Result := '';
end;
