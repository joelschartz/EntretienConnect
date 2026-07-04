@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0_EntretienConnect\EntretienConnect-Start.bat" (
  echo FEHLER: Der interne Ordner _EntretienConnect wurde nicht gefunden.
  echo Bitte die ZIP komplett entpacken und nichts aus dem Ordner verschieben.
  pause
  exit /b 1
)
call "%~dp0_EntretienConnect\EntretienConnect-Start.bat"
