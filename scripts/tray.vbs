' ===========================================================================
'  tray.vbs - start the system tray without a console window.
'
'  Double-click this file. It launches tray.py through pythonw.exe, so
'  nothing flashes on screen. This is the everyday entry point; the .bat
'  files are only for foreground debugging.
'
'  ---------------------------------------------------------------------
'  WHY THIS FILE IS PLAIN ASCII WITH CRLF ENDINGS:
'
'  Windows Script Host reads a .vbs as ANSI unless the file has a BOM.
'  A UTF-8 file with Chinese comments but no BOM is therefore parsed as
'  garbage and fails with "invalid character / 800A0408" - the script
'  will not start at all.  That is what bit us on 2026-09-28.
'  CRLF endings are kept as well even though this machine tolerates bare
'  LF (the AstrBot launcher next door uses LF); CRLF just removes the
'  question entirely and costs nothing.  Keep this file ASCII + CRLF.
'  The Chinese UI text lives in tray.py; this launcher needs none.
'  ---------------------------------------------------------------------
'
'  Any pythonw.exe will do: tray.py re-launches itself with an interpreter
'  that has pywin32 + Pillow, so we do not have to pick the right one here.
' ===========================================================================
Option Explicit

Dim sh, fso, root, script, py, cands, i

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' scripts\ -> project root
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
script = root & "\scripts\tray.py"

If Not fso.FileExists(script) Then
    MsgBox "Cannot find:" & vbCrLf & script, 16, "Local model service"
    WScript.Quit 1
End If

cands = Array( _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\pythonw.exe", _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe", _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe", _
    "C:\Python313\pythonw.exe", _
    "C:\Python312\pythonw.exe", _
    "C:\Python311\pythonw.exe")

py = ""
For i = 0 To UBound(cands)
    If fso.FileExists(cands(i)) Then
        py = cands(i)
        Exit For
    End If
Next

' Last resort: hope pythonw is on PATH. tray.py will swap in a usable
' interpreter by itself if this one lacks pywin32.
If py = "" Then py = "pythonw.exe"

' 0 = hidden window, False = do not wait
sh.Run """" & py & """ """ & script & """", 0, False
