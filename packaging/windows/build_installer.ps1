# SnapKey Vision AI Windows Installer Build Script

param (
    [bool]$CleanBuild = $true
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $ProjectRoot

$BuildScript = Join-Path $ProjectRoot "packaging\windows\build_windows.ps1"
$InstallerScript = Join-Path $ProjectRoot "packaging\windows\CameraAutomationInstaller.iss"
$ExpectedAppExe = Join-Path $ProjectRoot "dist\SnapKeyVisionAI\SnapKeyVisionAI.exe"
$ExpectedInstaller = Join-Path $ProjectRoot "dist\installer\MadhushalaCameraAISetup.exe"
$BuildInfo = Join-Path $ProjectRoot "camera_service\build_info.py"

# CI releases must embed immutable version/build metadata so the updater can
# distinguish the installed build from the published registry entry.
if ($env:GITHUB_SHA -and $env:GITHUB_RUN_NUMBER) {
    $EdgeVersion = "1.0.$($env:GITHUB_RUN_NUMBER)"
    $BuildInfoText = '"""Build metadata stamped by CI before packaging the Windows edge release."""' + [Environment]::NewLine +
        ('VERSION = "' + $EdgeVersion + '"') + [Environment]::NewLine +
        ('BUILD_ID = "' + $env:GITHUB_SHA + '"') + [Environment]::NewLine
    Set-Content -Path $BuildInfo -Value $BuildInfoText -Encoding UTF8
    $env:CAMERA_EDGE_VERSION = $EdgeVersion
    Write-Host "Stamped Camera Eye build metadata: $EdgeVersion / $($env:GITHUB_SHA)"
}

Write-Host "Building application package..."
& $BuildScript -CleanBuild $CleanBuild
if ($LASTEXITCODE -ne 0) {
    throw "Application build failed."
}

if (-not (Test-Path $ExpectedAppExe)) {
    throw "Expected application EXE not found: $ExpectedAppExe"
}

if ($env:GITHUB_SHA -and $env:GITHUB_RUN_NUMBER) {
    $ExpectedIdentity = "1.0.$($env:GITHUB_RUN_NUMBER) $($env:GITHUB_SHA)"
    $ActualIdentity = (& $ExpectedAppExe --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $ActualIdentity -ne $ExpectedIdentity) {
        throw "Packaged EXE identity mismatch. Expected '$ExpectedIdentity', got '$ActualIdentity'."
    }
    Write-Host "Verified packaged EXE identity: $ActualIdentity"
}

$IsccCommand = @(
    "ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | ForEach-Object {
    if ($_) {
        Get-Command $_ -ErrorAction SilentlyContinue
    }
} | Select-Object -First 1

if (-not $IsccCommand) {
    Write-Error @"
Inno Setup compiler was not found.

Install Inno Setup 6 on the build machine:
https://jrsoftware.org/isdl.php

Then run:
.\packaging\windows\build_installer.ps1
"@
    exit 1
}

$Iscc = $IsccCommand.Source
Write-Host "Building installer with Inno Setup: $Iscc"
& $Iscc $InstallerScript
if ($LASTEXITCODE -ne 0) {
    throw "Installer build failed."
}

if (-not (Test-Path $ExpectedInstaller)) {
    throw "Expected installer not found: $ExpectedInstaller"
}

$InstallerInfo = Get-Item $ExpectedInstaller
Write-Host "Installer created:"
Write-Host $ExpectedInstaller
Write-Host ("Size: {0:N2} MB" -f ($InstallerInfo.Length / 1MB))


# CI must prove that the installer itself contains and installs the exact build.
# This catches stale payloads and silent-upgrade regressions before publishing.
if ($env:GITHUB_SHA -and $env:GITHUB_RUN_NUMBER) {
    $SmokeDir = Join-Path $env:RUNNER_TEMP "camera-eye-installer-smoke"
    if (Test-Path $SmokeDir) {
        Remove-Item $SmokeDir -Recurse -Force
    }
    $InstallerArgs = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/NOAUTOSTART",
        ("/DIR=" + $SmokeDir)
    )
    $Setup = Start-Process -FilePath $ExpectedInstaller -ArgumentList $InstallerArgs -Wait -PassThru
    if ($Setup.ExitCode -ne 0) {
        throw "Installer smoke test failed with exit code $($Setup.ExitCode)."
    }

    $InstalledExe = Join-Path $SmokeDir "SnapKeyVisionAI.exe"
    if (-not (Test-Path $InstalledExe)) {
        throw "Installer smoke test did not install SnapKeyVisionAI.exe."
    }
    $ExpectedIdentity = "1.0.$($env:GITHUB_RUN_NUMBER) $($env:GITHUB_SHA)"
    $InstalledIdentity = (& $InstalledExe --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $InstalledIdentity -ne $ExpectedIdentity) {
        throw "Installed EXE identity mismatch. Expected '$ExpectedIdentity', got '$InstalledIdentity'."
    }
    Write-Host "Verified installer payload identity: $InstalledIdentity"

    Get-Process SnapKeyVisionAI -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
