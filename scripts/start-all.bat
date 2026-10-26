@echo off
REM ===========================================================================
REM  start-all.bat - foreground debug mode
REM
REM  Only to make logs easy to read: the bridge stays in this window and
REM  Ctrl+C ends it.  For everyday use, double-click scripts\tray.vbs.
REM
REM  The real startup logic is in scripts\service_ctl.py (single source of
REM  truth); this file only locates Python.
REM
REM  Keep this file pure ASCII + CRLF (see stop-all.bat for the reason).
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

"%PY%" "scripts\service_ctl.py" start --console
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
