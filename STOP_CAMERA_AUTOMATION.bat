@echo off
SETLOCAL

REM SnapKey Vision AI Stop Script
REM This script stops the SnapKey Vision AI application

echo Stopping SnapKey Vision AI...

taskkill /f /im SnapKeyVisionAI.exe >nul 2>&1

echo SnapKey Vision AI stopped.
pause

ENDLOCAL
