Option Explicit
Dim shell, fso, folder, helper, starter, stopCmd, i
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

folder = fso.GetParentFolderName(WScript.ScriptFullName)
helper = folder & "\_EntretienConnect"
starter = helper & "\EntretienConnect-Start-Hidden.bat"

Function ServerReady()
  On Error Resume Next
  Dim h
  Set h = CreateObject("WinHttp.WinHttpRequest.5.1")
  h.Open "GET", "http://127.0.0.1:8765/api/graph/capabilities", False
  h.SetTimeouts 800, 800, 800, 800
  h.Send
  If Err.Number = 0 And h.Status = 200 Then
    ServerReady = True
  Else
    ServerReady = False
  End If
  Err.Clear
  On Error GoTo 0
End Function

Sub ShowStartupLog()
  On Error Resume Next
  Dim logPath
  logPath = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\EntretienConnect\EntretienConnect-startup-output.txt"
  If fso.FileExists(logPath) Then
    shell.Run "notepad.exe " & Chr(34) & logPath & Chr(34), 1, False
  End If
  On Error GoTo 0
End Sub

If Not fso.FolderExists(helper) Then
  MsgBox "Der interne Ordner _EntretienConnect wurde nicht gefunden." & vbCrLf & vbCrLf & _
         "Bitte die ZIP zuerst komplett entpacken und nichts aus dem Ordner verschieben.", vbCritical, "EntretienConnect"
  WScript.Quit 1
End If

If Not fso.FileExists(helper & "\EntretienConnect.ps1") Then
  MsgBox "EntretienConnect.ps1 wurde nicht gefunden." & vbCrLf & vbCrLf & _
         "Bitte die ZIP zuerst komplett entpacken und diese Datei im entpackten Ordner starten.", vbCritical, "EntretienConnect"
  WScript.Quit 1
End If

If Not fso.FileExists(starter) Then
  MsgBox "EntretienConnect-Start-Hidden.bat wurde nicht gefunden." & vbCrLf & vbCrLf & _
         "Bitte die ZIP komplett entpacken.", vbCritical, "EntretienConnect"
  WScript.Quit 1
End If

' Alte EntretienConnect-PowerShell-Prozesse beenden, damit kein alter Server/Port aktiv bleibt.
stopCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command " & Chr(34) & _
          "$me=$PID; Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $me -and ($_.CommandLine -like '*EntretienConnect.ps1*' -or $_.CommandLine -like '*EntretienConnect-Start-Hidden.bat*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" & Chr(34)
shell.Run stopCmd, 0, True
WScript.Sleep 700

' Wichtig: Der VBS-Starter öffnet den Browser NICHT selbst.
' Das macht EntretienConnect.ps1 erst, nachdem der lokale Server wirklich lauscht.
shell.CurrentDirectory = helper
shell.Run Chr(34) & starter & Chr(34), 0, False

' Nur prüfen, ob der Server hochkommt. Kein Browser-Öffnen aus VBS heraus,
' damit kein verfrühtes 127.0.0.1/ERR_CONNECTION_REFUSED-Tab entsteht.
For i = 1 To 100
  WScript.Sleep 500
  If ServerReady() Then
    WScript.Quit 0
  End If
Next

MsgBox "EntretienConnect konnte den lokalen Server nicht starten." & vbCrLf & vbCrLf & _
       "Ich öffne jetzt die technische Logdatei. Bitte schicke ihren Inhalt oder starte die Diagnose-Datei.", _
       vbExclamation, "EntretienConnect"
ShowStartupLog
