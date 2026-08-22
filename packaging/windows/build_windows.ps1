# Camera Automation Windows Build Script
# Fixed version for proper PyInstaller build

param (
    [string]$BuildType = "onedir",  # "onedir" or "onefile" - configured in spec
    [string]$OutputDir = "dist",
    [bool]$CleanBuild = $true,
    [bool]$IncludeDebugSymbols = $false
)

# Configuration
$ProjectName = "CameraAutomation"
$SpecFile = "packaging/windows/CameraAutomation.spec"
# Ensure we're in the project root
$ProjectRoot = $PSScriptRoot + "\..\.."
Set-Location $ProjectRoot

$RepoVenvPython = Join-Path $ProjectRoot ".venv311\Scripts\python.exe"
if (Test-Path $RepoVenvPython) {
    $PythonPath = $RepoVenvPython
} else {
    $PythonPath = "python"
}

# Clean previous build if requested
if ($CleanBuild) {
    Write-Host "Cleaning previous build..."
    if (Test-Path "$OutputDir") {
        Remove-Item "$OutputDir" -Recurse -Force
    }
    if (Test-Path "build") {
        Remove-Item "build" -Recurse -Force
    }
}

# Create output directory
if (-not (Test-Path "$OutputDir")) {
    New-Item -ItemType Directory -Path "$OutputDir" | Out-Null
}

# Check if PyInstaller is available
try {
    & $PythonPath -m pip show pyinstaller | Out-Null
} catch {
    Write-Host "PyInstaller not found. Installing..."
    & $PythonPath -m pip install pyinstaller
}

# Build the application using the spec file directly
Write-Host "Building Camera Automation using PyInstaller spec file..."

# Build command - use the spec file as the single source of truth
$BuildArgs = @(
    "--clean",
    "--noconfirm"
)

try {
    Write-Host "Running PyInstaller with spec file: $SpecFile"
    & $PythonPath -m PyInstaller @BuildArgs $SpecFile

    # Check exit code immediately
    if ($LASTEXITCODE -ne 0) {
        Write-Error "PyInstaller build failed with exit code $LASTEXITCODE"
        exit $LASTEXITCODE
    }

    Write-Host "Build completed."

    # Verify the expected output exists
    $ExpectedExePath = Join-Path $OutputDir "$ProjectName\CameraAutomation.exe"

    if (Test-Path $ExpectedExePath) {
        $ExeInfo = Get-Item $ExpectedExePath
        $ExeSize = $ExeInfo.Length / 1MB
        Write-Host "Build successful! Executable created at: $ExpectedExePath"
        Write-Host "EXE size: $($ExeSize.ToString('F2')) MB"

        # Create start/stop scripts
        $StartScriptContent = @"
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
"@

        $StopScriptContent = @"
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
"@

        # Write start/stop scripts
        $StartScriptContent | Out-File -FilePath "START_CAMERA_AUTOMATION.bat" -Encoding utf8
        $StopScriptContent | Out-File -FilePath "STOP_CAMERA_AUTOMATION.bat" -Encoding utf8

        Write-Host "Created START_CAMERA_AUTOMATION.bat and STOP_CAMERA_AUTOMATION.bat"

        # Create .env.example file if it doesn't exist
        if (-not (Test-Path ".env.example")) {
            $EnvExampleContent = @"
# Camera Automation Configuration
# Copy this file to .env and modify as needed

# Server Configuration
HOST=127.0.0.1
PORT=8000

# Data Directory (Windows)
DATA_DIR=C:\ProgramData\CameraAutomation

# Logging
LOG_LEVEL=INFO

# Feature Flags
FACE_RECOGNITION_ENABLED=true
ATTENDANCE_ENABLED=true
UNKNOWN_DETECTION_ENABLED=true

# Face Recognition Settings
FACE_KNOWN_THRESHOLD=0.65
FACE_REQUIRED_OBSERVATIONS=3

# Unknown Person Settings
UNKNOWN_CONFIRM_SECONDS=3

# Auto-open browser on startup
AUTO_OPEN_BROWSER=true

# Debug mode
DEBUG=false
"@

            $EnvExampleContent | Out-File -FilePath ".env.example" -Encoding utf8
            Write-Host "Created .env.example file"
        }

        Write-Host "Build process completed successfully!"
        exit 0
    } else {
        Write-Error "Build failed. Expected executable not found at: $ExpectedExePath"
        exit 1
    }
} catch {
    Write-Error "Build failed with error: $_"
    exit 1
}
