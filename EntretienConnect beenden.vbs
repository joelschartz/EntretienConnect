Option Explicit
Dim shell, cmd
Set shell = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command " & Chr(34) & _
      "$me=$PID; Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $me -and ($_.CommandLine -like '*EntretienConnect.ps1*' -or $_.CommandLine -like '*EntretienConnect-Start-Hidden.bat*' -or $_.CommandLine -like '*EntretienConnect-Start.bat*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Remove-Item -Path ($env:LOCALAPPDATA+'\EntretienConnect\EntretienConnect-startup-output.txt') -Force -ErrorAction SilentlyContinue" & Chr(34)
shell.Run cmd, 0, True
MsgBox "EntretienConnect wurde beendet, falls es im Hintergrund lief.", vbInformation, "EntretienConnect"
