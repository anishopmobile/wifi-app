#define MyAppName "AniWiFi"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "AniShop"
#define MyAppExeName "AniWiFi.exe"

[Setup]
AppId={{AECBD2A1-C4BD-465B-AB0C-4946AA5D47AA}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\AniWiFi
DefaultGroupName=AniWiFi
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\installer-output
OutputBaseFilename=AniWiFi-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\AniWiFi\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AniWiFi"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\AniWiFi"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "اجرای آنی وای‌فای"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Customer files are deliberately NOT removed on uninstall.
Type: files; Name: "{app}\*.log"
