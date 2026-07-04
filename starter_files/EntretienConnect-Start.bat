@echo off
setlocal
cd /d "%~dp0"
title EntretienConnect v174 - Start mit Diagnose

echo ============================================================
echo EntretienConnect v174 - sichtbarer Start / Diagnose
echo Ordner: %CD%
echo ============================================================
echo.

if not exist "%~dp0EntretienConnect.ps1" (
  echo FEHLER: EntretienConnect.ps1 wurde nicht gefunden.
  echo Bitte die ZIP zuerst komplett entpacken und nicht direkt aus der ZIP starten.
  echo.
  pause
  exit /b 1
)

set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSEXE%" set "PSEXE=powershell.exe"

echo PowerShell: %PSEXE%
echo Script: %~dp0EntretienConnect.ps1
echo.
"%PSEXE%" -NoLogo -NoProfile -Command "$PSVersionTable.PSVersion"
echo.
echo Starte EntretienConnect GitHub-Starter auf http://127.0.0.1:8765/graph.html ...
echo.
echo Falls hier ein roter Fehler erscheint: bitte Screenshot vom ganzen Fenster schicken.
echo Dieses Fenster offen lassen, solange die App benutzt wird.
echo.

"%PSEXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0EntretienConnect.ps1"

set "ERR=%ERRORLEVEL%"
echo.
echo ============================================================
echo EntretienConnect wurde beendet oder konnte nicht starten.
echo Exit-Code: %ERR%
echo Logdatei, falls vorhanden: %LOCALAPPDATA%\EntretienConnect\EntretienConnect-log.txt
echo Startup-Log: %LOCALAPPDATA%\EntretienConnect\EntretienConnect-startup-output.txt
echo ============================================================
echo.
pause
exit /b %ERR%
