; Build only with build-windows-installer.ps1: payload is the verified ZIP.
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif

[Setup]
AppId={{29724728-3CC7-434A-8290-D9B60E814B71}
AppName=Habab Home
AppVersion={#AppVersion}
AppPublisher=Habab
DefaultDirName={localappdata}\HababHome
DefaultGroupName=Habab Home
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=habab_home_assistant
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
InfoBeforeFile=before-install.txt
UninstallDisplayName=Habab Home
UninstallDisplayIcon={sys}\shell32.dll,12

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Habab Home"; Filename: "{app}\start-windows.bat"; WorkingDir: "{app}"; IconFilename: "{sys}\shell32.dll"; IconIndex: 12
Name: "{autodesktop}\Habab Home"; Filename: "{app}\start-windows.bat"; WorkingDir: "{app}"; IconFilename: "{sys}\shell32.dll"; IconIndex: 12; Tasks: desktopicon
Name: "{group}\{cm:UninstallProgram,Habab Home}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\start-windows.bat"; Description: "{cm:LaunchProgram,Habab Home}"; WorkingDir: "{app}"; Flags: shellexec nowait postinstall skipifsilent

; No Docker commands or [UninstallDelete]: the home's named volumes stay intact.
