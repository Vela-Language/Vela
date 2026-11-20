; Vela installer (Inno Setup 6)
; Build:
;   ISCC.exe /DMyAppVersion=0.1.0 /DMyAppDisplayVersion=0.1.0 /DMyAppBuild=0 installer\vela.iss
; VersionInfoVersion must stay numeric x.y.z.build; the display version may carry -dev.N.

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#ifndef MyAppDisplayVersion
  #define MyAppDisplayVersion MyAppVersion
#endif
#ifndef MyAppBuild
  #define MyAppBuild "0"
#endif

#define MyAppName "Vela"
#define MyAppPublisher "MARKJY-China"
#define MyAppURL "https://vela.markjy.com/"
#define MyAppSupportURL "https://github.com/Vela-Language/Vela"

[Setup]
AppId={{6E2B7C1F-4A3D-4C8E-9B71-2F5D8A0C4E93}
AppName={#MyAppName}
AppVersion={#MyAppDisplayVersion}
AppVerName={#MyAppName} {#MyAppDisplayVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppSupportURL}
AppUpdatesURL={#MyAppSupportURL}/releases
DefaultDirName={localappdata}\Vela
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=vela-{#MyAppDisplayVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
ChangesEnvironment=yes
VersionInfoVersion={#MyAppVersion}.{#MyAppBuild}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=Vela compiler installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
UninstallDisplayName={#MyAppName} {#MyAppDisplayVersion}
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\vela\*"; DestDir: "{app}\vela"; Excludes: "__pycache__\*,*.pyc"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\vela.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\examples\*"; DestDir: "{app}\examples"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\docs\*"; DestDir: "{app}\docs"; Flags: recursesubdirs createallsubdirs ignoreversion

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}"; Check: NeedsAddPath('{app}'); Flags: preservestringtype

[Code]
function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';',
                ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure RemoveFromPath();
var
  OrigPath, NewPath, Part, UpperApp: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
    exit;
  UpperApp := Uppercase(ExpandConstant('{app}'));
  NewPath := '';
  while OrigPath <> '' do
  begin
    P := Pos(';', OrigPath);
    if P = 0 then
    begin
      Part := OrigPath;
      OrigPath := '';
    end
    else
    begin
      Part := Copy(OrigPath, 1, P - 1);
      Delete(OrigPath, 1, P);
    end;
    if (Part <> '') and (Uppercase(Part) <> UpperApp) then
    begin
      if NewPath <> '' then
        NewPath := NewPath + ';';
      NewPath := NewPath + Part;
    end;
  end;
  RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', NewPath);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath();
end;
