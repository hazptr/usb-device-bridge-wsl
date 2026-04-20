; Inno Setup 6 script — builds a setup program from the PyInstaller onedir output.
; Install Inno Setup from https://jrsoftware.org/isinfo.php
; From repo root, after `py -m PyInstaller UsbipdWslAttach.spec`:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\UsbipdWslAttach.iss
; Or: .\scripts\build_installer.ps1

#define MyAppName "USB/IP to WSL Attach Manager"
#define MyAppVersion "0.1.0"
#define MyAppExeName "UsbipdWslAttach.exe"
#define MyAppPublisher "usbipd-device-attach-manager"

[Setup]
AppId={{8F3E2D1C-4B5A-6978-90AB-CDEF01234567}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf64}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist-installer
OutputBaseFilename=UsbipdWslAttach-Setup-{#MyAppVersion}
SetupIconFile=..\assets\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
VersionInfoVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\UsbipdWslAttach\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "{#MyAppName}"
Name: "{autoprograms}\{#MyAppName}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
