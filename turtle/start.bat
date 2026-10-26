@echo off
REM ===========================================================================
REM  start.bat - Turtle Soup game, one-click start
REM
REM  Does three things: start the llama.cpp backend -> start the judging
REM  service -> open the browser. The service runs in THIS window (so you can
REM  see its log); closing the window stops the game service. The llama.cpp
REM  backend keeps running in the background (stop it with scripts\stop-all.bat
REM  or the tray menu).
REM
REM  --host 0.0.0.0 makes the service reachable from Docker containers via
REM  host.docker.internal:11440 (AstrBot runs in Docker). Without it the
REM  server binds loopback only and the container gets connection refused.
REM
REM  To switch model or mode, edit the arguments on the last command below:
REM     --model qwen35-4b-heretic   less VRAM (2.7GB), binary accuracy 87.1%
REM     --model qwen3-8b            default, binary accuracy 93.5% (~5.2GB VRAM)
REM     --mode  three               keep the "unrelated" label (lower accuracy)
REM
REM  *** KEEP THIS FILE PURE ASCII + CRLF ***
REM  cmd.exe parses a .bat byte by byte using the active code page. Non-ASCII
REM  bytes (e.g. Chinese comments) shift its line offsets -- especially around
REM  chcp -- and it starts gluing the next line's REM onto the current command.
REM  Bare LF is likewise unreliable. Chinese text belongs in server.py and
REM  index.html, never here. Verify with: python scripts\check_scripts_env.py
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

echo [1/3] Starting llama.cpp backend...
"%PY%" "scripts\service_ctl.py" start

echo [2/3] Starting judging service (browser opens automatically)...
echo [3/3] Close this window to stop the game service.
echo.
"%PY%" "turtle\server.py" --host 0.0.0.0 --port 11440 --model qwen3-8b --mode binary --open-browser

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
