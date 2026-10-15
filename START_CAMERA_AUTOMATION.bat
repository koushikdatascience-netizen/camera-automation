@echo off
SETLOCAL

REM Madhushala Camera AI Start Script

SET APP_DIR=%~dp0
SET APP_NAME=SnapKeyVisionAI.exe
SET AUTO_OPEN_BROWSER=0
SET PORT=8091
SET CAMERA_AUTOMATION_HOME=%ProgramData%\MadhushalaCameraAI

echo Starting Madhushala Camera AI...
echo Application Directory: %APP_DIR%

cd /d "%APP_DIR%"

if exist "%APP_NAME%" (
    start "" /min "%APP_NAME%" --background
    echo Madhushala Camera AI started successfully.
    timeout /t 3 /nobreak >nul
    start "" "http://127.0.0.1:8091/setup"
) else (
    echo Error: %APP_NAME% not found in %APP_DIR%
    pause
)

ENDLOCAL
