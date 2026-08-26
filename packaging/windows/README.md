# SnapKey Vision AI Windows Client Installer

This folder builds a professional Windows installer for client testing.

The client should receive only:

```text
SnapKeyVisionAISetup.exe
```

They should not need Git, source code, a Python install, or command-line clone steps.

## Build Machine Requirements

Install these only on your internal build PC:

```text
Python 3.11 64-bit
Inno Setup 6
```

Then install Python dependencies once:

```powershell
cd "C:\path\to\camera-automation"
py -3.11 -m venv .venv311
.\.venv311\Scripts\python.exe -m pip install --upgrade pip
.\.venv311\Scripts\python.exe -m pip install -r requirements.txt
```

Inno Setup 6:

```text
https://jrsoftware.org/isdl.php
```

## Build Final Installer

Run from the project root:

```powershell
.\packaging\windows\build_installer.ps1
```

Output:

```text
dist\installer\SnapKeyVisionAISetup.exe
```

That single EXE is what you send to the client.

## What The Installer Includes

The installer packages:

```text
SnapKeyVisionAI.exe
Python runtime packed by PyInstaller
Ultralytics/YOLO dependencies from the build environment
OpenCV, FastAPI, Uvicorn, InsightFace, ONNX Runtime
Web UI files
yolo11n.pt model
Start/stop shortcuts
```

Persistent client data is stored outside the install folder:

```text
C:\ProgramData\SnapKeyVisionAI
```

This keeps database, evidence, snapshots, clips, and config writable without exposing a source checkout.

## Client Install/Test Flow

On the client machine:

1. Run `SnapKeyVisionAISetup.exe`.
2. Click Install.
3. Launch **SnapKey Vision AI** from desktop/start menu.
4. Browser opens:

```text
http://127.0.0.1:8091/setup
```

5. Add webcam/camera.
6. Enable required features.
7. Open Tracking and test bounding boxes/alerts.

## Notes About Code Protection

PyInstaller makes deployment professional, but it is not strong source-code protection. A technical user can still reverse engineer Python bytecode with enough effort.

For stronger commercial protection later:

```text
Nuitka compiled build
Code signing certificate
License activation tied to machine code
Server-side paid feature checks
Model/config encryption
Private cloud sync for paid deployments
```

For current client demos, this installer is the right next step because it removes Git/Python/source-code setup from the client workflow.
