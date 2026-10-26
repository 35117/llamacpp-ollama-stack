@echo off
REM ===========================================================================
REM  apply-personas.bat
REM
REM  Re-extract the persona system prompts from config\modelfiles\*
REM  (Modelfiles) into config\personas\, then regenerate config\personas.json.
REM
REM  Use this after you edit a Modelfile or add an entry to the PERSONAS table
REM  in scripts\extract_personas.py.
REM
REM  Afterwards restart the service: scripts\stop-all.bat  then  scripts\start-all.bat
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
  goto :fail
)

echo.
"%PY%" "scripts\extract_personas.py"
if errorlevel 1 goto :fail

echo.
echo === validate llama-swap config =======================================
"bin\llama-swap.exe" -config "config\llama-swap.yaml" -validate

echo.
echo === current callable models / personas ===============================
"%PY%" "scripts\list_models.py"

echo.
echo A restart is needed before the change takes effect:
echo    scripts\stop-all.bat   then   scripts\start-all.bat
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

:fail
popd
echo.
pause
exit /b 1
