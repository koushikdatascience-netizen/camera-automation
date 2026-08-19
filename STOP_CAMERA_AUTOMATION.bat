@echo off
SETLOCAL

REM Camera Automation Stop Script
REM This script stops the Camera Automation application

echo Stopping Camera Automation...

taskkill /f /im CameraAutomation.exe >nul 2>&1
taskkill /f /im python.exe >nul 2>&1

echo Camera Automation stopped.
pause

ENDLOCAL
