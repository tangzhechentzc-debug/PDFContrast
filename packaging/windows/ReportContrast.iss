#define AppName "ReportContrast"
#define AppVersion "0.2.0"

[Setup]
AppId={{10E43045-1EB6-4066-84FE-FD24A5592ACF}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\ReportContrast
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\ReportContrast.exe
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
AppMutex=Local\ReportContrast.Desktop.7763E5EB
CloseApplications=no
OutputDir={#SetupOutput}
OutputBaseFilename=ReportContrast-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#AppBundle}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\ReportContrast"; Filename: "{app}\ReportContrast.exe"; WorkingDir: "{app}"
