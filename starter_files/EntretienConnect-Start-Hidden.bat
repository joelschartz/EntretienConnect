@echo off
cd /d "%~dp0"
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PSEXE%" set "PSEXE=powershell.exe"
set "LOGDIR=%LOCALAPPDATA%\EntretienConnect"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>nul
echo === EntretienConnect v191 hidden start %DATE% %TIME% ===>"%LOGDIR%\EntretienConnect-startup-output.txt"
echo Ordner: %CD%>>"%LOGDIR%\EntretienConnect-startup-output.txt"
echo PowerShell: %PSEXE%>>"%LOGDIR%\EntretienConnect-startup-output.txt"
"%PSEXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0EntretienConnect.ps1" >>"%LOGDIR%\EntretienConnect-startup-output.txt" 2>&1
echo.>>"%LOGDIR%\EntretienConnect-startup-output.txt"
echo === Prozess beendet: %DATE% %TIME% ===>>"%LOGDIR%\EntretienConnect-startup-output.txt"
