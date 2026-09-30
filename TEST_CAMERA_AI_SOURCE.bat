@echo off
setlocal
title Camera AI - Source Test
cd /d "%~dp0"

echo Camera AI source test
echo Close the installed Camera AI application before continuing.
echo This test uses your existing ProgramData configuration and local records.
echo.

set "CAMERA_AUTOMATION_HOME=%ProgramData%\MadhushalaCameraAI"
set "CAMERA_AUTOMATION_CONFIG=%CAMERA_AUTOMATION_HOME%\config.yaml"
set "PYTHONUTF8=1"
set "AUTO_OPEN_BROWSER=1"
set "PYTHON_EXE=python"
if exist "%~dp0.venv311\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv311\Scripts\python.exe"

if not exist "%CAMERA_AUTOMATION_CONFIG%" (
    echo Installed config.yaml was not found. Source was not started.
    pause
    exit /b 1
)

powershell.exe -NoProfile -Command "if (@(Get-NetTCPConnection -LocalPort 8091 -State Listen -ErrorAction SilentlyContinue).Count -gt 0) { exit 1 }; exit 0"
if errorlevel 1 (
    echo Port 8091 is already in use. Close the installed service and try again.
    echo No running application has been stopped by this launcher.
    pause
    exit /b 1
)

echo Starting SOURCE. Keep this window open while testing.
echo Browser address: http://127.0.0.1:8091/setup
echo Press Ctrl+C here when you have finished testing.
echo.
"%PYTHON_EXE%" -m camera_service.launcher --host 127.0.0.1 --port 8091
echo.
echo Source service has stopped. Any startup error is shown above.
pause
endlocal
