@echo off
REM ===========================================================================
REM  status.bat - services, GPU, loaded models, and every callable model name.
REM
REM  All the logic lives in scripts\status.py: cmd's `find` and parenthesised
REM  echo statements break when Git's bin directory is on PATH, so the batch
REM  file only locates Python and hands over.
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
  popd
  pause
  exit /b 1
)

"%PY%" "scripts\status.py"
echo.
echo (Press any key to close)
pause >nul
popd
endlocal
exit /b 0

:trypy
if defined PY goto :eof
if not exist %1 goto :eof
%1 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
goto :eof
