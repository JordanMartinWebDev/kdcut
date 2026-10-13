; Inno Setup script for KDcut. Built by GitHub Actions:
;   iscc /DAppVersion=0.2.N packaging\installer.iss
#ifndef AppVersion
  #define AppVersion "0.5.0"
#endif

[Setup]
AppId={{8F3C2B71-6A5E-4D2B-9C1A-3E7D5B2A9F10}
AppName=KDcut
AppVersion={#AppVersion}
AppPublisher=KDPlays
AppPublisherURL=https://github.com/JordanMartinWebDev/kdcut
DefaultDirName={localappdata}\Programs\KDcut
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=KDcut-Setup
SetupIconFile=kdcut.ico
UninstallDisplayIcon={app}\KDcut.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; Remove files from the previous version so upgrades are clean.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\KDcut\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\KDcut"; Filename: "{app}\KDcut.exe"
Name: "{autodesktop}\KDcut"; Filename: "{app}\KDcut.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KDcut.exe"; Description: "Open KDcut"; Flags: nowait postinstall skipifsilent
; In-app updates run the installer silently; reopen KDcut when they finish.
Filename: "{app}\KDcut.exe"; Flags: nowait; Check: WizardSilent
