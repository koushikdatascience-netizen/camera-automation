@echo off
SETLOCAL

REM Camera Automation Start Script
REM This script starts the Camera Automation application

SET APP_DIR=%~dp0
SET APP_NAME=CameraAutomation.exe

echo Starting Camera Automation...
echo Application Directory: %APP_DIR%

cd /d "%APP_DIR%"

if exist "%APP_NAME%" (
    start "" "%APP_NAME%"
    echo Camera Automation started successfully.
    echo Opening browser to setup page...
    timeout /t 2 /nobreak >nul
    start "" "http://127.0.0.1:8000/setup"
) else (
    echo Error: %APP_NAME% not found in %APP_DIR%
    pause
)

ENDLOCAL
