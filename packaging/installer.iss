; =====================================================================
; FaceClock - Windows installer (Inno Setup 6)
;   iscc /DAppVersion=1.1.0 packaging\installer.iss
; Expects the PyInstaller output in dist\FaceClock
; =====================================================================
#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif

[Setup]
AppId={{6C1E3B2A-7F4D-4B8E-9A51-FACEC10C0001}
AppName=FaceClock
AppVersion={#AppVersion}
AppPublisher=FARHAT Mohamad
AppPublisherURL=https://github.com/FarhatMohamd/faceclock
AppSupportURL=https://github.com/FarhatMohamd/faceclock/issues
DefaultDirName={autopf}\FaceClock
DefaultGroupName=FaceClock
; per-user install: no administrator rights needed
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=FaceClock-Setup-{#AppVersion}
SetupIconFile=faceclock.ico
UninstallDisplayIcon={app}\FaceClock.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\FaceClock\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\FaceClock"; Filename: "{app}\FaceClock.exe"
Name: "{group}\{cm:UninstallProgram,FaceClock}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\FaceClock"; Filename: "{app}\FaceClock.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\FaceClock.exe"; Description: "{cm:LaunchProgram,FaceClock}"; Flags: nowait postinstall skipifsilent

; The user's data (%APPDATA%\FaceClock: database, settings) is kept on uninstall on purpose.
