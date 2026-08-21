@echo off
setlocal EnableExtensions
title Hoferium - Start

REM ============================================================
REM  hoferium.bat - Starter fuer Hoferium
REM
REM  Beim ersten Start richtet sich alles automatisch ein:
REM    1. Administrator-Rechte anfordern
REM    2. Python finden - oder automatisch installieren
REM    3. Umgebung + Oberflaeche (customtkinter) einrichten
REM    4. Programm starten
REM  Ab dem zweiten Start geht es direkt zu Schritt 4.
REM ============================================================

REM --- Pfad dieser Datei ueber Env-Variable (robust gegen Apostrophe) ---
set "HOFERIUM_SELF=%~f0"
set "HOFERIUM_DIR=%~dp0"

REM --- 1) Admin-Rechte sicherstellen ---
REM  Der angemeldete Benutzer wird mitgegeben: Bestaetigt jemand den UAC-Dialog
REM  mit einem ANDEREN Konto, laeuft alles unter dessen Profil - dann wuerde
REM  das falsche (leere) Profil gesichert. Das Programm warnt in dem Fall.
if not "%~1"=="" set "HOFERIUM_CALLER=%~1"
net session >nul 2>&1
if %errorlevel% neq 0 goto elevate
if not defined HOFERIUM_CALLER set "HOFERIUM_CALLER=%USERNAME%"
goto have_admin

:elevate
REM  Benutzername als Argument mitgeben (in Anfuehrungszeichen ueber [char]34,
REM  damit auch Namen mit Leerzeichen heil ankommen).
echo Fordere Administrator-Rechte an ...
powershell -NoProfile -Command "try{ Start-Process -FilePath $env:HOFERIUM_SELF -Verb RunAs -ArgumentList ([char]34 + $env:USERNAME + [char]34) } catch { exit 1 }"
REM  Diese Pruefung steht bewusst AUSSERHALB eines Klammerblocks - dort waere
REM  %errorlevel% schon beim Einlesen ersetzt worden und immer veraltet.
if %errorlevel% neq 0 (
    echo(
    echo [ABGEBROCHEN] Ohne Administrator-Rechte geht es nicht weiter.
    echo Bitte den Windows-Dialog mit "Ja" bestaetigen.
    echo(
    pause
)
exit /b

:have_admin

set "VENV=%LOCALAPPDATA%\Hoferium\venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"

REM --- Ist die Umgebung fertig UND passt sie zur Programmversion? ---
REM  Geprueft wird die Marker-Datei, NICHT pythonw.exe: die entsteht schon
REM  beim Anlegen der Umgebung, also bevor customtkinter installiert ist.
REM  Im Marker steht die Version, fuer die eingerichtet wurde. Nach einem
REM  Update mit neuen Abhaengigkeiten stimmt sie nicht mehr - dann werden
REM  die Pakete nachgezogen, statt mit einer unpassenden Umgebung zu starten.
set "WANTVER="
if exist "%HOFERIUM_DIR%VERSION" set /p WANTVER=<"%HOFERIUM_DIR%VERSION"
set "HAVEVER="
if exist "%VENV%\.ready" set /p HAVEVER=<"%VENV%\.ready"
if not exist "%VENV%\.ready" goto freshsetup
if "%HAVEVER%"=="%WANTVER%" goto run
goto refresh

:freshsetup
if exist "%VENV%" rd /s /q "%VENV%" >nul 2>&1

echo(
echo =====================================================
echo   Hoferium - Ersteinrichtung (nur beim ersten Mal)
echo =====================================================
echo(

REM --- 2) Basis-Python suchen ---
call :findpython
if defined BASEPY goto makevenv

REM --- 3) Kein Python -> automatisch installieren ---
echo Python nicht gefunden - Installation wird gestartet ...
where winget >nul 2>&1
if %errorlevel%==0 goto winget_py
goto download_py

:winget_py
echo Installiere Python 3.12 via winget ...
winget install --id Python.Python.3.12 -e --scope machine --accept-package-agreements --accept-source-agreements
if %errorlevel% neq 0 (
    echo winget hat nicht funktioniert - versuche den Direkt-Download ...
    goto download_py
)
goto after_py

:download_py
echo Lade offiziellen Python-Installer von python.org ...
powershell -NoProfile -Command "try{ Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile ($env:TEMP+'\pysetup.exe') } catch { exit 1 }"
if %errorlevel% neq 0 (
    del "%TEMP%\pysetup.exe" >nul 2>&1
    echo(
    echo [FEHLER] Python-Download fehlgeschlagen - bitte Internetverbindung pruefen.
    echo(
    pause
    exit /b 1
)
"%TEMP%\pysetup.exe" /quiet InstallAllUsers=1 PrependPath=1 Include_tcltk=1 Include_pip=1
del "%TEMP%\pysetup.exe" >nul 2>&1
goto after_py

:after_py
call :findpython
if defined BASEPY goto makevenv
REM  PATH ist im laufenden Fenster noch alt - bekannte Installationsorte direkt pruefen
if exist "%ProgramFiles%\Python312\python.exe" set BASEPY="%ProgramFiles%\Python312\python.exe"
if not defined BASEPY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set BASEPY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined BASEPY goto makevenv
echo(
echo Python wurde installiert, ist in diesem Fenster aber noch nicht auffindbar.
echo Bitte hoferium.bat NOCH EINMAL starten (der Suchpfad wird erst in einem
echo neuen Fenster aktualisiert). Kommt diese Meldung erneut, hat die
echo Python-Installation nicht geklappt.
echo(
pause
exit /b 0

:makevenv
echo Richte Python-Umgebung ein ...
%BASEPY% -m venv "%VENV%"
if not exist "%VPY%" (
    echo [FEHLER] Konnte die Python-Umgebung nicht anlegen.
    pause
    exit /b 1
)
echo Aktualisiere pip ...
"%VPY%" -m pip install --upgrade pip
call :installdeps
"%VPY%" -c "import customtkinter" >nul 2>&1
if %errorlevel% neq 0 (
    echo(
    echo [FEHLER] Installation der Oberflaeche fehlgeschlagen - Internet pruefen.
    echo Die unvollstaendige Umgebung wird entfernt, damit der naechste Start
    echo sauber neu einrichtet.
    rd /s /q "%VENV%" >nul 2>&1
    pause
    exit /b 1
)
REM  Erst JETZT als fertig markieren - mit der Version, fuer die es gilt
>"%VENV%\.ready" echo %WANTVER%
echo Einrichtung abgeschlossen.
goto run

REM ------------------------------------------------------------
REM  Nach einem Update: Abhaengigkeiten der neuen Fassung nachziehen.
REM ------------------------------------------------------------
:refresh
echo(
echo Neue Programmversion erkannt (%HAVEVER% -^> %WANTVER%).
echo Pruefe die benoetigten Pakete ...
call :installdeps
"%VPY%" -c "import customtkinter" >nul 2>&1
if %errorlevel% neq 0 (
    echo(
    echo [FEHLER] Die Pakete liessen sich nicht aktualisieren.
    echo Bitte Internetverbindung pruefen und erneut starten.
    pause
    exit /b 1
)
>"%VENV%\.ready" echo %WANTVER%
echo Bereit.
goto run

REM ------------------------------------------------------------
REM  Installiert die benoetigten Pakete. Liegt eine requirements.txt
REM  bei, hat sie Vorrang - so kann eine kuenftige Fassung weitere
REM  Pakete anfordern, ohne dass dieser Starter sie kennen muss.
REM ------------------------------------------------------------
:installdeps
if exist "%HOFERIUM_DIR%requirements.txt" goto reqfile
echo Installiere Oberflaeche (customtkinter) ...
"%VPY%" -m pip install --upgrade customtkinter
goto :eof
:reqfile
echo Installiere Pakete aus requirements.txt ...
"%VPY%" -m pip install --upgrade -r "%HOFERIUM_DIR%requirements.txt"
goto :eof

:run
set "HOFERIUM_HOME=%HOFERIUM_DIR%"
cd /d "%HOFERIUM_DIR%"
call :hideclutter
if exist "%VPYW%" (
    start "" /D "%HOFERIUM_DIR%" "%VPYW%" -m nucleus
) else (
    "%VPY%" -m nucleus
)
exit /b

REM ------------------------------------------------------------
REM  Blendet alles aus, was zum Programm gehoert. Sichtbar bleiben
REM  nur hoferium.bat und LIESMICH.txt - die Dateien funktionieren
REM  weiter, das Attribut betrifft nur die Anzeige im Explorer.
REM ------------------------------------------------------------
:hideclutter
pushd "%HOFERIUM_DIR%"
if errorlevel 1 goto :eof
for %%I in (nucleus docs tools .git .github .gitignore .gitattributes README.md LICENSE VERSION apps.json requirements.txt update.json _vorherige_version) do if exist "%%I" attrib +h "%%I" >nul 2>&1
popd
goto :eof

REM ------------------------------------------------------------
REM  Sucht einen Basis-Python-Interpreter und setzt BASEPY.
REM ------------------------------------------------------------
:findpython
set "BASEPY="
py -3 --version >nul 2>&1 && set "BASEPY=py -3"
if not defined BASEPY (
    py --version >nul 2>&1 && set "BASEPY=py"
)
if not defined BASEPY (
    python --version >nul 2>&1 && set "BASEPY=python"
)
goto :eof
