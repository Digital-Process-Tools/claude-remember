' windows-hidden-run.vbs (#1002)
'
' Runs a command hidden -- no console window -- via WshShell.Run with
' window style 0 and wait=False. Invoked as:
'   wscript.exe //B windows-hidden-run.vbs ARG0 ARG1 ...
' ARG0 is the executable, the rest are its own arguments, each already a
' separate WScript.Arguments entry (split by the calling process, not by
' this script). WshShell.Run takes one command-line string, so building
' it correctly is this script's one job.
'
' QuoteArg implements the Microsoft C-runtime argv-splitting convention
' CreateProcess itself expects (the one Python's own stdlib
' subprocess.list2cmdline implements, and that tests/test_windows_hidden_
' detach_1002.py pins this function against): a literal quote inside an
' argument must be escaped as backslash-quote, and a run of backslashes
' is only special immediately before a quote, where it must be doubled
' so it is not itself read as escaping that quote. A bare doubled quote
' ("") inside a quoted argument is NOT a literal quote under this
' convention -- it is two quote-mode toggles with nothing between them --
' so the first version of this script (Replace(arg, chr(34), chr(34) &
' chr(34))) silently dropped every quote it was trying to preserve,
' corrupting the "$0" "$@" quoting lib-detach.sh relies on for every
' hidden-launch call whenever an argument contains a literal quote
' (#1002 review).
Function QuoteArg(arg)
    Dim needQuote, result, i, c, bsCount
    needQuote = (InStr(arg, " ") > 0) Or (InStr(arg, vbTab) > 0) Or (Len(arg) = 0)
    result = ""
    bsCount = 0
    For i = 1 To Len(arg)
        c = Mid(arg, i, 1)
        If c = "\" Then
            bsCount = bsCount + 1
        ElseIf c = Chr(34) Then
            result = result & String(bsCount * 2 + 1, "\") & Chr(34)
            bsCount = 0
        Else
            If bsCount > 0 Then
                result = result & String(bsCount, "\")
                bsCount = 0
            End If
            result = result & c
        End If
    Next
    If needQuote Then
        result = result & String(bsCount * 2, "\")
        QuoteArg = Chr(34) & result & Chr(34)
    Else
        result = result & String(bsCount, "\")
        QuoteArg = result
    End If
End Function

Dim objArgs, objShell, cmdLine, i
Set objArgs = WScript.Arguments
cmdLine = ""
For i = 0 To objArgs.Count - 1
    If i > 0 Then
        cmdLine = cmdLine & " "
    End If
    cmdLine = cmdLine & QuoteArg(objArgs.Item(i))
Next

Set objShell = CreateObject("WScript.Shell")
objShell.Run cmdLine, 0, False
