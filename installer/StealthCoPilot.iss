; Stealth Co-Pilot - Windows installer (Inno Setup 6).
;
; Build:  ISCC.exe installer\StealthCoPilot.iss /DAppVersion=1.2.3
; Needs:  dist\StealthInterviewCoPilot.exe (the PyInstaller build from the workflow)
;
; The app installs per-user under %LOCALAPPDATA%\Programs so the in-app updater can
; replace its own EXE without a UAC prompt on every release. User data lives in
; %LOCALAPPDATA%\StealthInterviewCoPilot and is deliberately left in place on
; uninstall (interview history, downloaded speech model, settings).

#ifndef AppVersion
  #define AppVersion "1.1.7"
#endif

#define AppName "Stealth Co-Pilot"
#define AppExe "StealthInterviewCoPilot.exe"
#define AppPublisher "Siddharth Deshmukh"
#define AppUrl "https://stealth-co-pilot.vercel.app"

[Setup]
AppId={{8F4C1B62-6D2E-4B4A-9A3E-0C5F5B7D9E21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
AppUpdatesURL={#AppUrl}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=no
LicenseFile=LICENSE.txt
OutputDir=..\dist
OutputBaseFilename=StealthInterviewCoPilot-Setup-v{#AppVersion}
SetupIconFile=..\assets\stealth_copilot.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"
Name: "quicklaunchicon"; Description: "Create a Quick Launch icon"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\stealth_copilot.ico"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
; {userdesktop} follows a OneDrive-redirected desktop; the public {autodesktop}
; folder is invisible to people whose Desktop lives in OneDrive, which is why the
; shortcut looked missing after install.
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The wizard's own leftovers only; %LOCALAPPDATA%\StealthInterviewCoPilot stays.
Type: filesandordirs; Name: "{app}\assets"

[Code]
// Report where the shortcuts went, so "the desktop icon is missing" is answerable
// from the install log instead of guessing.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    Log('Desktop folder: ' + ExpandConstant('{userdesktop}'));
end;

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nThe app keeps its updates, interview history and speech model in your own user folder, and it updates itself in place.%n%nIt is recommended that you close all other applications before continuing.
