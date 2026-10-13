; Installateur du Traducteur d'écran (Inno Setup 6), fabriqué par construire_exe.bat à partir de
; dist\TraducteurEcran. Installe pour l'utilisateur seul (pas besoin d'être administrateur), crée le
; raccourci du menu Démarrer, et se désinstalle proprement depuis les Paramètres de Windows.

#define Nom "Traducteur d'écran"
#ifndef Version
  #define Version "2.0.0"
#endif

[Setup]
AppId={{6F1C2A3E-8B7D-4E2F-9A1C-5D3B7E9F0A12}
AppName={#Nom}
AppVersion={#Version}
AppVerName={#Nom} {#Version}
AppPublisher={#Nom}
DefaultDirName={localappdata}\Programs\TraducteurEcran
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=.
OutputBaseFilename=TraducteurEcran-Installation
SetupIconFile=icone.ico
UninstallDisplayIcon={app}\TraducteurEcran.exe
UninstallDisplayName={#Nom}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Une version déjà lancée est fermée avant d'être remplacée (mise à jour).
CloseApplications=force
RestartApplications=no

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "bureau"; Description: "Créer un raccourci sur le Bureau"; Flags: unchecked

[Files]
Source: "dist\TraducteurEcran\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; (Les fichiers d'une version précédente qui n'existent plus ne restent pas.)
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#Nom}"; Filename: "{app}\TraducteurEcran.exe"
Name: "{autodesktop}\{#Nom}"; Filename: "{app}\TraducteurEcran.exe"; Tasks: bureau

[Run]
Filename: "{app}\TraducteurEcran.exe"; Description: "Lancer le Traducteur d'écran"; Flags: nowait postinstall

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM TraducteurEcran.exe /F"; Flags: runhidden; RunOnceId: "ArretTraducteur"

[Registry]
; « Lancer au démarrage de Windows » (réglage du programme) : retiré à la désinstallation.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "TraducteurEcran"; Flags: uninsdeletevalue
