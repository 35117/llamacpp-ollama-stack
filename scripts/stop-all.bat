@echo off
REM ===========================================================================
REM  stop-all.bat - stop the bridge, llama-swap and every llama-server child
REM
REM  The real logic lives in scripts\service_ctl.py (single source of truth).
REM  The tray menu's "Stop service" calls the very same thing.
REM
REM  Keep this file pure ASCII + CRLF: cmd.exe parses .bat byte by byte using
REM  the active code page, so non-ASCII comments shift its line offsets and it
REM  ends up gluing the next line onto the current command.  Chinese text in
REM  output belongs in the .py scripts.
REM ===========================================================================
setlocal EnableExtensions
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "ROOT=%~dp0.."
pushd "%ROOT%" || exit /b 1

set "PY="
call :trypy "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :trypy "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :trypy "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY call :trypy "%%i"
  )
)
if not defined PY (
  echo [X] Python 3.8+ not found.
  goto :end
)

"%PY%" "scripts\service_ctl.py" stop
goto :end

:trypy
if defined PY goto :eof
if not exist %1 goto :eof
%1 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
goto :eof

:end
popd
echo.
echo (Press any key to close)
pause >nul
endlocal
exit /b 0
