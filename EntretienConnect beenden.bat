@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$me=$PID; Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $me -and ($_.CommandLine -like '*EntretienConnect.ps1*' -or $_.CommandLine -like '*EntretienConnect-Start-Hidden.bat*' -or $_.CommandLine -like '*EntretienConnect-Start.bat*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Remove-Item -Path ($env:LOCALAPPDATA+'\EntretienConnect\EntretienConnect-startup-output.txt') -Force -ErrorAction SilentlyContinue"
echo EntretienConnect wurde beendet, falls es im Hintergrund lief.
pause
