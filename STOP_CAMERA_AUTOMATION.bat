@echo off
SETLOCAL

REM Madhushala Camera AI Stop Script

echo Stopping Madhushala Camera AI...

taskkill /f /im SnapKeyVisionAI.exe >nul 2>&1

echo Madhushala Camera AI stopped.
pause

ENDLOCAL
