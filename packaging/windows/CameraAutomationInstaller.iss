#define MyAppName "SnapKey Vision AI"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "SnapKey"
#define MyAppExeName "SnapKeyVisionAI.exe"

[Setup]
AppId={{9C45FCA7-21AA-426F-8D63-62B61586191C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\SnapKey Vision AI
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=SnapKeyVisionAISetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
SetupIconFile=..\..\camera_service\web\favicon.ico
; When final brand assets are provided, enable these:
; SetupIconFile=..\..\assets\brand\app.ico
; WizardImageFile=..\..\assets\brand\installer-wizard.bmp
; WizardSmallImageFile=..\..\assets\brand\installer-banner.bmp
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Dirs]
Name: "{commonappdata}\SnapKeyVisionAI"; Permissions: users-modify
Name: "{commonappdata}\SnapKeyVisionAI\data"; Permissions: users-modify
Name: "{commonappdata}\SnapKeyVisionAI\data\evidence"; Permissions: users-modify

[Files]
Source: "..\..\dist\SnapKeyVisionAI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SnapKey Vision AI"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Stop SnapKey Vision AI"; Filename: "{app}\STOP_SNAPKEY_VISION_AI.bat"; WorkingDir: "{app}"
Name: "{autodesktop}\SnapKey Vision AI"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce
Name: "launchafterinstall"; Description: "Launch SnapKey Vision AI after installation"; GroupDescription: "After install:"; Flags: checkedonce

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch SnapKey Vision AI"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent; Tasks: launchafterinstall
