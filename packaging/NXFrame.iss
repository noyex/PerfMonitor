; Inno Setup script of the installer. Build (after PyInstaller and with the window at dist\NXFrame.exe):
;   iscc /DAppVersion=0.1.0 packaging\NXFrame.iss   ->  dist\NXFrame-Setup-0.1.0.exe
; The in-app updater (perfmon/updater.py) runs it with /SILENT: the same steps without the wizard pages.
#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z
#endif

[Setup]
AppId={{6F1C3E52-8B0A-4D7E-9C55-2A7B1E4D9F31}
AppName=NX Frame
AppVersion={#AppVersion}
AppPublisher=noyex
AppPublisherURL=https://github.com/noyex/PerfMonitor
AppUpdatesURL=https://github.com/noyex/PerfMonitor/releases
DefaultDirName={autopf}\NX Frame
; always Program Files: the collector task runs the program elevated, so only administrators may write to its folder
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=NXFrame-Setup-{#AppVersion}
SetupIconFile=..\perfmon\ui\static\favicon.ico
UninstallDisplayIcon={app}\NXFrame.exe
UninstallDisplayName=NX Frame
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; the collector and the window are stopped in PrepareToInstall, not through Restart Manager
CloseApplications=no

[Languages]
Name: "pl"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
pl.DesktopIcon=Skrót na pulpicie
en.DesktopIcon=Desktop shortcut
pl.Sensors=Zainstaluj LibreHardwareMonitor i PresentMon (winget): temperatury, zegary i klatki w grach
en.Sensors=Install LibreHardwareMonitor and PresentMon (winget): temperatures, clocks and game frames
pl.Launch=Uruchom NX Frame
en.Launch=Launch NX Frame
pl.Collector=Uruchamianie kolektora...
en.Collector=Starting the collector...
pl.Winget=Instalowanie LibreHardwareMonitor i PresentMon...
en.Winget=Installing LibreHardwareMonitor and PresentMon...

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"
Name: "sensors"; Description: "{cm:Sensors}"; Check: not WizardSilent

[Files]
Source: "..\dist\NXFrame\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\NXFrame.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "collector-task.ps1"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; a previous version's runtime: PyInstaller file names change between Python / package versions
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\NX Frame"; Filename: "{app}\NXFrame.exe"
Name: "{autodesktop}\NX Frame"; Filename: "{app}\NXFrame.exe"; Tasks: desktopicon

[Run]
; --scope machine: under Program Files, where the elevated collector looks for them (perfmon/config.py).
; Through cmd: winget is an App Execution Alias found on PATH only (a missing winget just fails this step)
Filename: "{cmd}"; Parameters: "/c winget install --exact --id LibreHardwareMonitor.LibreHardwareMonitor --scope machine --silent --accept-package-agreements --accept-source-agreements --disable-interactivity"; StatusMsg: "{cm:Winget}"; Tasks: sensors; Flags: runhidden
Filename: "{cmd}"; Parameters: "/c winget install --exact --id Intel.PresentMon.Console --scope machine --silent --accept-package-agreements --accept-source-agreements --disable-interactivity"; StatusMsg: "{cm:Winget}"; Tasks: sensors; Flags: runhidden
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\collector-task.ps1"" -AppDir ""{app}"""; StatusMsg: "{cm:Collector}"; Flags: runhidden
; After an update (silent) the window comes back on its own; after a wizard install it is a checkbox. Through the
; shell: the updater starts Setup already elevated, where runasoriginaluser has no original user to go back to, and
; the window must never run elevated
Filename: "{win}\explorer.exe"; Parameters: """{app}\NXFrame.exe"""; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent
Filename: "{win}\explorer.exe"; Parameters: """{app}\NXFrame.exe"""; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\collector-task.ps1"" -AppDir ""{app}"" -Remove"; Flags: runhidden; RunOnceId: "RemoveTask"

[Code]
// Stops the collector task and every running exe of the program before files are replaced (they hold _internal
// open). The script comes from this installer, not from the version being replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  ExtractTemporaryFile('collector-task.ps1');
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\collector-task.ps1') + '" -AppDir "' +
       ExpandConstant('{app}') + '" -Stop', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
