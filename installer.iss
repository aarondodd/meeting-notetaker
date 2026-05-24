; Inno Setup script for Meeting Notetaker.
;
; Wraps the PyInstaller-built dist\meeting-notetaker.exe in a proper
; Windows installer: Start Menu entry, Add/Remove Programs registration,
; per-user install with no admin required (user can elevate via the UAC
; dialog for a system-wide install if they want).
;
; Compile via: ISCC.exe /DAppVersion=0.6.6 installer.iss
; The CI workflow passes /DAppVersion derived from the release tag.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
; Stable AppId so future installers upgrade in place instead of stacking
; side-by-side entries in Add/Remove Programs. Do NOT change this value.
AppId={{B1F03D8E-7C29-4A6E-9B0F-9A6B7C0E1D2F}}
AppName=Meeting Notetaker
AppVersion={#AppVersion}
AppVerName=Meeting Notetaker {#AppVersion}
AppPublisher=Aaron Dodd
AppPublisherURL=https://github.com/aarondodd/meeting-notetaker
AppSupportURL=https://github.com/aarondodd/meeting-notetaker/issues
AppUpdatesURL=https://github.com/aarondodd/meeting-notetaker/releases
DefaultDirName={autopf}\MeetingNotetaker
DefaultGroupName=Meeting Notetaker
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=Output
OutputBaseFilename=meeting-notetaker-setup-{#AppVersion}
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; lowest = per-user by default, no admin prompt. The override dialog
; lets the user elevate for a Program Files install if they pick it.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayName=Meeting Notetaker {#AppVersion}
UninstallDisplayIcon={app}\meeting-notetaker.exe
MinVersion=10.0.17763
; CloseApplications + RestartApplications make Windows Restart Manager
; close the running meeting-notetaker.exe before the install, then
; relaunch it after. Required for the in-app self-updater (utils/
; updater.py) which downloads this same installer and runs it silently;
; without these directives a silent upgrade would fail with "file in use"
; when the existing install is being replaced.
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; PyInstaller --onedir produces dist\meeting-notetaker\ with the launcher
; .exe plus several hundred sibling .pyd / .dll / data files (torch,
; speechbrain, PyQt6, faster-whisper). Recurse the whole tree into {app}
; preserving the layout the launcher expects to find.
Source: "dist\meeting-notetaker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Meeting Notetaker"; Filename: "{app}\meeting-notetaker.exe"
Name: "{group}\{cm:UninstallProgram,Meeting Notetaker}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Meeting Notetaker"; Filename: "{app}\meeting-notetaker.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\meeting-notetaker.exe"; Description: "{cm:LaunchProgram,Meeting Notetaker}"; Flags: nowait postinstall skipifsilent
