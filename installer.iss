; Nazzel for Windows: per-user installer (no administrator rights needed).
#define AppVersion GetEnv("NAZZEL_VERSION")
#if AppVersion == ""
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6E1E2B57-3C4D-4A39-9E0A-6F5D4C9B1A11}
AppName=Nazzel
AppVerName=Nazzel {#AppVersion}
AppVersion={#AppVersion}
AppPublisher=Nazzel
DefaultDirName={localappdata}\Programs\Nazzel
DefaultGroupName=Nazzel
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=Nazzel-Setup
SetupIconFile=nazzel_desktop\ui\icon.ico
UninstallDisplayIcon={app}\Nazzel.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\Nazzel\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Nazzel"; Filename: "{app}\Nazzel.exe"
Name: "{group}\{cm:UninstallProgram,Nazzel}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Nazzel"; Filename: "{app}\Nazzel.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Nazzel.exe"; Description: "{cm:LaunchProgram,Nazzel}"; Flags: nowait postinstall skipifsilent
