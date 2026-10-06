# SnapKey Vision AI Windows Build Script
# Fixed version for proper PyInstaller build

param (
    [string]$BuildType = "onedir",  # "onedir" or "onefile" - configured in spec
    [string]$OutputDir = "dist",
    [bool]$CleanBuild = $true,
    [bool]$IncludeDebugSymbols = $false
)

# Configuration
$ProjectName = "SnapKeyVisionAI"
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
    $RootPath = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') + '\'
    foreach ($Target in @((Join-Path $OutputDir $ProjectName), 'build')) {
        $TargetPath = [IO.Path]::GetFullPath((Join-Path $ProjectRoot $Target))
        if (-not $TargetPath.StartsWith($RootPath, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing cleanup outside the repository: $TargetPath"
        }
        if (Test-Path -LiteralPath $TargetPath) {
            Remove-Item -LiteralPath $TargetPath -Recurse -Force -ErrorAction Stop
        }
    }
}

# Create output directory
if (-not (Test-Path "$OutputDir")) {
    New-Item -ItemType Directory -Path "$OutputDir" | Out-Null
}

# Check if PyInstaller is available
& $PythonPath -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller not found. Installing..."
    & $PythonPath -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        Write-Error "PyInstaller installation failed. Install it manually with: $PythonPath -m pip install pyinstaller"
        exit $LASTEXITCODE
    }
}

# Build the application using the spec file directly
Write-Host "Building SnapKey Vision AI using PyInstaller spec file..."

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
    $ExpectedExePath = Join-Path $OutputDir "$ProjectName\SnapKeyVisionAI.exe"

    if (Test-Path $ExpectedExePath) {
        $ExeInfo = Get-Item $ExpectedExePath
        $ExeSize = $ExeInfo.Length / 1MB
        Write-Host "Build successful! Executable created at: $ExpectedExePath"
        Write-Host "EXE size: $($ExeSize.ToString('F2')) MB"

        $AppDistDir = Split-Path $ExpectedExePath -Parent
        $InternalDir = Join-Path $AppDistDir "_internal"
        $RequiredFiles = @(
            (Join-Path $InternalDir "config.example.yaml")
        )
        $OptionalFiles = @(
            (Join-Path $InternalDir "yolo11m.pt"),
            (Join-Path $InternalDir "yolo26n.pt"),
            (Join-Path $InternalDir "kaggle-model\scissors_yolo11m_960.pt")
        )

        foreach ($RequiredFile in $RequiredFiles) {
            if (-not (Test-Path $RequiredFile)) {
                Write-Error "Build verification failed. Required packaged file missing: $RequiredFile"
                exit 1
            }
        }
        foreach ($OptionalFile in $OptionalFiles) {
            if (-not (Test-Path $OptionalFile)) {
                Write-Warning "Optional packaged file missing: $OptionalFile"
            }
        }
        Write-Host "Verified required default config; optional model assets are reported when absent."

        # Create start/stop/open scripts inside the installable app folder
        $StartScriptContent = @"
@echo off
SETLOCAL

REM Madhushala Camera AI Start Script

SET APP_DIR=%~dp0
SET APP_NAME=SnapKeyVisionAI.exe
SET AUTO_OPEN_BROWSER=0
SET PORT=8091
SET CAMERA_AUTOMATION_HOME=%ProgramData%\MadhushalaCameraAI

echo Starting Madhushala Camera AI in background...
echo Application Directory: %APP_DIR%

cd /d "%APP_DIR%"

if exist "%APP_NAME%" (
    tasklist /FI "IMAGENAME eq %APP_NAME%" | find /I "%APP_NAME%" >nul
    if errorlevel 1 (
        start "" /min "%APP_NAME%" --background
        timeout /t 3 /nobreak >nul
    )
    start "" "http://127.0.0.1:8091/setup"
) else (
    echo Error: %APP_NAME% not found in %APP_DIR%
    pause
)

ENDLOCAL
"@

$StopScriptContent = @"
@echo off
SETLOCAL

REM Madhushala Camera AI Stop Script

echo Stopping Madhushala Camera AI...

taskkill /f /im SnapKeyVisionAI.exe >nul 2>&1

echo Madhushala Camera AI stopped.

ENDLOCAL
"@

        $OpenScriptContent = @"
@echo off
SETLOCAL

REM Open Madhushala Camera AI local dashboard.

SET APP_DIR=%~dp0
SET APP_NAME=SnapKeyVisionAI.exe
SET AUTO_OPEN_BROWSER=0
SET PORT=8091
SET CAMERA_AUTOMATION_HOME=%ProgramData%\MadhushalaCameraAI

cd /d "%APP_DIR%"

tasklist /FI "IMAGENAME eq %APP_NAME%" | find /I "%APP_NAME%" >nul
if errorlevel 1 (
    start "" /min "%APP_NAME%" --background
    timeout /t 3 /nobreak >nul
)

start "" "http://127.0.0.1:8091/setup"

ENDLOCAL
"@

        # Write start/stop scripts
        $StartScriptContent | Out-File -FilePath (Join-Path $AppDistDir "START_MADHUSHALA_CAMERA_AI.bat") -Encoding ascii
        $StopScriptContent | Out-File -FilePath (Join-Path $AppDistDir "STOP_MADHUSHALA_CAMERA_AI.bat") -Encoding ascii
        $OpenScriptContent | Out-File -FilePath (Join-Path $AppDistDir "OPEN_MADHUSHALA_CAMERA_AI.bat") -Encoding ascii
        $StartScriptContent | Out-File -FilePath (Join-Path $AppDistDir "START_SNAPKEY_VISION_AI.bat") -Encoding ascii
        $StopScriptContent | Out-File -FilePath (Join-Path $AppDistDir "STOP_SNAPKEY_VISION_AI.bat") -Encoding ascii

        Write-Host "Created Madhushala Camera AI start/stop/open scripts in $AppDistDir"

        # Create .env.example file if it doesn't exist
        if (-not (Test-Path ".env.example")) {
            $EnvExampleContent = @"
# SnapKey Vision AI Configuration
# Copy this file to .env and modify as needed

# Server Configuration
HOST=127.0.0.1
PORT=8091

# Data Directory (Windows)
DATA_DIR=C:\ProgramData\SnapKeyVisionAI

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
