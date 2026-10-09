' windows-hidden-run.vbs (#1002)
'
' Runs a command hidden -- no console window -- via WshShell.Run with
' window style 0 and wait=False. Invoked as:
'   wscript.exe //B windows-hidden-run.vbs ARG0 ARG1 ...
' ARG0 is the executable, the rest are its own arguments. Each argument
' arrives as a separate WScript.Arguments entry (already split by the
' calling process, not by this script), so building the single command
' line WshShell.Run itself requires is this script's one job: wrap each
' argument in quotes, doubling any quote already inside it, and join with
' spaces.
Dim objArgs, objShell, cmdLine, i, arg

Set objArgs = WScript.Arguments
cmdLine = ""
For i = 0 To objArgs.Count - 1
    arg = objArgs.Item(i)
    arg = Replace(arg, Chr(34), Chr(34) & Chr(34))
    If i > 0 Then
        cmdLine = cmdLine & " "
    End If
    cmdLine = cmdLine & Chr(34) & arg & Chr(34)
Next

Set objShell = CreateObject("WScript.Shell")
objShell.Run cmdLine, 0, False
