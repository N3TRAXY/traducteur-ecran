@echo off
rem Fabrique TraducteurEcran-Windows.zip (version .exe, sans Python) a partir de traducteur_ecran.pyw.
rem Le travail se fait dans un dossier temporaire : PyInstaller n'aime pas l'apostrophe du nom de ce dossier.
setlocal
set TRAVAIL=%TEMP%\construction_traducteur
set ICI=%~dp0
if exist "%TRAVAIL%" rmdir /s /q "%TRAVAIL%"
mkdir "%TRAVAIL%"
copy /y "%ICI%traducteur_ecran.pyw" "%TRAVAIL%\TraducteurEcran.pyw" >nul
copy /y "%APPDATA%\TraducteurEcran\icone.ico" "%TRAVAIL%\icone.ico" >nul
cd /d "%TRAVAIL%"
py -m pip install --quiet pyinstaller -r "%ICI%requirements.txt"
py -m PyInstaller --noconfirm --windowed --name TraducteurEcran --icon icone.ico ^
  --collect-all customtkinter --collect-all ctranslate2 --collect-all py3langid --exclude-module lingua --collect-data argostranslate ^
  --collect-all sacremoses --collect-all sentencepiece --collect-data wordfreq --collect-all dxcam --collect-submodules comtypes --hidden-import pystray._win32 ^
  --exclude-module torch --exclude-module spacy --exclude-module stanza --exclude-module scipy ^
  --exclude-module onnxruntime --exclude-module thinc --exclude-module blis --exclude-module minisbd ^
  --exclude-module transformers --exclude-module pandas --exclude-module matplotlib --exclude-module IPython ^
  TraducteurEcran.pyw || goto :erreur
copy /y "%ICI%LISEZ-MOI (pour les amis).txt" "dist\TraducteurEcran\LISEZ-MOI.txt" >nul
rem Extension du navigateur (traduction directement dans les pages), a charger dans le navigateur.
xcopy /e /i /y /q "%ICI%extension" "dist\TraducteurEcran\extension" >nul
rem La meme pour Firefox : son manifeste a en plus l'identifiant que Firefox exige (Chrome le signale comme une erreur).
xcopy /e /i /y /q "%ICI%extension" "dist\TraducteurEcran\extension-firefox" >nul
py -c "import json; c='dist/TraducteurEcran/extension-firefox/manifest.json'; m=json.load(open(c, encoding='utf-8')); m['browser_specific_settings']={'gecko': {'id': '{303484cc-3607-4b0a-8576-1ea9bf60a675}', 'strict_min_version': '140.0', 'data_collection_permissions': {'required': ['none']}}}; json.dump(m, open(c, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)" || goto :erreur
rem Version et depot GitHub, lus dans le programme.
py -c "import re;s=open('TraducteurEcran.pyw',encoding='utf-8').read();q=chr(34);print(re.search('VERSION_PROGRAMME = '+q+'([^'+q+']+)',s).group(1))" > version.txt || goto :erreur
py -c "import re;s=open('TraducteurEcran.pyw',encoding='utf-8').read();q=chr(34);print(re.search('DEPOT_GITHUB = '+q+'([^'+q+']*)',s).group(1) or 'aucun')" > depot.txt || goto :erreur
set /p VERSION=<version.txt
set /p DEPOT=<depot.txt
rem Grand modele de traduction anglais -> francais (OPUS-MT, licence CC-BY 4.0) : telecharge au premier lancement
rem depuis GitHub quand un depot est configure (paquet bien plus leger) ; sinon, livre avec le programme.
if "%DEPOT%"=="aucun" if exist "%APPDATA%\TraducteurEcran\modeles\en_fr\model.bin" xcopy /e /i /y /q "%APPDATA%\TraducteurEcran\modeles\en_fr" "dist\TraducteurEcran\modeles\en_fr" >nul
rem (Chemin du .zip passe par une variable : l'apostrophe de « Traducteur d'écran » cassait la commande.)
set SORTIE=%ICI%TraducteurEcran-Windows.zip
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\TraducteurEcran' -DestinationPath $env:SORTIE" || goto :erreur
rem Installateur (Inno Setup) : TraducteurEcran-Installation.exe.
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" goto :sans_installateur
copy /y "%ICI%installateur.iss" "%TRAVAIL%\installateur.iss" >nul
"%ISCC%" /Q /DVersion=%VERSION% "%TRAVAIL%\installateur.iss" || goto :erreur
copy /y "%TRAVAIL%\TraducteurEcran-Installation.exe" "%ICI%TraducteurEcran-Installation.exe" >nul
:sans_installateur
echo.
echo Termine : TraducteurEcran-Installation.exe et TraducteurEcran-Windows.zip sont prets (version %VERSION%).
pause
exit /b 0
:erreur
echo La construction a echoue.
pause
exit /b 1
