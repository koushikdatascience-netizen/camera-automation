#define MyAppName "Madhushala Camera AI"
#define EnvAppVersion GetEnv("CAMERA_EDGE_VERSION")
#if EnvAppVersion == ""
  #define MyAppVersion "1.0.0"
#else
  #define MyAppVersion EnvAppVersion
#endif
#define MyAppPublisher "Madhushala Software"
#define MyAppExeName "SnapKeyVisionAI.exe"

[Setup]
AppId={{9C45FCA7-21AA-426F-8D63-62B61586191C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Madhushala Camera AI
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=MadhushalaCameraAISetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
CloseApplicationsFilter=SnapKeyVisionAI.exe
RestartApplications=no
PrivilegesRequired=admin
SetupIconFile=..\..\assets\brand\app.ico
WizardImageFile=..\..\assets\brand\installer-wizard.bmp
WizardSmallImageFile=..\..\assets\brand\installer-banner.bmp
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Dirs]
Name: "{commonappdata}\MadhushalaCameraAI"; Permissions: users-modify
Name: "{commonappdata}\MadhushalaCameraAI\data"; Permissions: users-modify
Name: "{commonappdata}\MadhushalaCameraAI\data\evidence"; Permissions: users-modify

[Files]
Source: "..\..\dist\SnapKeyVisionAI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "_internal\torch-*.dist-info\licenses\*,_internal\*.dist-info\licenses\third_party\*,_internal\**\__pycache__\*,_internal\**\*.pyc"

[Icons]
Name: "{group}\Madhushala Camera AI"; Filename: "{app}\OPEN_MADHUSHALA_CAMERA_AI.bat"; WorkingDir: "{app}"
Name: "{group}\Start Madhushala Camera AI"; Filename: "{app}\START_MADHUSHALA_CAMERA_AI.bat"; WorkingDir: "{app}"
Name: "{group}\Stop Madhushala Camera AI"; Filename: "{app}\STOP_MADHUSHALA_CAMERA_AI.bat"; WorkingDir: "{app}"
Name: "{autodesktop}\Madhushala Camera AI"; Filename: "{app}\OPEN_MADHUSHALA_CAMERA_AI.bat"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce
Name: "launchafterinstall"; Description: "Launch Madhushala Camera AI after installation"; GroupDescription: "After install:"; Flags: checkedonce

[Run]
Filename: "{app}\START_MADHUSHALA_CAMERA_AI.bat"; Description: "Launch Madhushala Camera AI"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent; Tasks: launchafterinstall
Filename: "{app}\{#MyAppExeName}"; Parameters: "--background"; WorkingDir: "{app}"; Flags: nowait runhidden; Check: WizardSilent

[Registry]
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SnapKeyVisionAI"; ValueData: """{app}\{#MyAppExeName}"" --background"; Flags: uninsdeletevalue

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  { AppMutex previously caused silent upgrades to exit before replacing files.
    Stop the background edge process explicitly before [Files] runs. }
  Exec(ExpandConstant('{sys}\taskkill.exe'),
       '/F /IM SnapKeyVisionAI.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(750);
  Result := '';
end;
