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
$ExpectedInstaller = Join-Path $ProjectRoot "dist\installer\SnapKeyVisionAISetup.exe"

Write-Host "Building application package..."
& $BuildScript -CleanBuild $CleanBuild
if ($LASTEXITCODE -ne 0) {
    throw "Application build failed."
}

if (-not (Test-Path $ExpectedAppExe)) {
    throw "Expected application EXE not found: $ExpectedAppExe"
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
