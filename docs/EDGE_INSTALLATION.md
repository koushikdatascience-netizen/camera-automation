# Windows Edge Installation And Upgrade

## Build Machine

Use Python 3.11 repository virtual environment and Inno Setup 6. Build is manual; the release work does not run a long installer build automatically.

Required source assets: `yolo26n.pt`, `yolo11m.pt`, `kaggle-model/scissors_yolo11m_960.pt`, brand assets, web assets and config.example.yaml. Offline face models `det_10g.onnx` and `w600k_r50.onnx` must exist in the build user's `.insightface/models/buffalo_l`. The spec fails if required face/scissors files are absent. Optional `yolo26n.onnx` and `yolo26n_openvino_model` are included when present. Models and generated build output are not committed by this release.
The retrained scissors file has been placed at the required path on the build machine and class-checked as `scissors`. Keep this file in place for every build; `jewellery_tag_best.pt` has only a `jewellery_tag` class and is not a substitute. Models remain local build assets, not Git-tracked source.

```powershell
cd "D:\Madhushala Software\Camera ai\camera-automation"
git switch feat/camera-eye-live-view-ux
git pull --ff-only
.\packaging\windows\build_installer.ps1
Get-FileHash .\dist\installer\MadhushalaCameraAISetup.exe -Algorithm SHA256
```

Close any process running from the build's `dist\SnapKeyVisionAI` before building. Cleanup is constrained to repository build paths. It does not delete customer ProgramData.

## Client

Installer: `dist\installer\MadhushalaCameraAISetup.exe`.
Application: `dist\SnapKeyVisionAI\SnapKeyVisionAI.exe` in the build, installed under Program Files\Madhushala Camera AI by default. Older installations may use Program Files\SnapKey Vision AI.

Clients do not install Git, Python or libraries. The ONEDIR runtime, detector and required face/object models are included by the packaging spec. Internet is needed for activation/sync, not baseline local inference after activation within license/grace validity.

Open the application shortcut, or:

```powershell
Start-Process -FilePath "C:\Program Files\Madhushala Camera AI\SnapKeyVisionAI.exe" -ArgumentList "--port 8091" -WindowStyle Hidden
Start-Process "http://127.0.0.1:8091/setup"
Invoke-RestMethod http://127.0.0.1:8091/health
```

Generate a one-time code in the authenticated cloud portal for the correct shop. Production no longer accepts the shared global activation code as an edge onboarding credential. Existing native login remains; new production owner account provisioning requires the trusted CRM/backend key server-side, never embedded in browser JavaScript. Use POST /crm/session for trusted CRM launches.

## Upgrade Safety

Persistent directory: `C:\ProgramData\MadhushalaCameraAI`. Preserve config.yaml, data\camera_automation.db, data\license_cache.json, evidence and logs. Back up this directory before a pilot upgrade. Use the same installer AppId. Do not uninstall/delete ProgramData to resolve application file locks.

Close only SnapKeyVisionAI, install the new package, restart and verify health plus existing camera/license data. Do not kill all Python processes. If an application lock prevents upgrade, exit Camera Eye before continuing; never ignore file replacement errors.

The installer registers background startup at Windows logon; a frozen single-instance mutex prevents a second copy from running. No Windows service is installed. Validate startup after reboot/login on the actual PC. --background suppresses the browser; --open-ui opens the local setup page.

## Release Artifact Checks

Verify `_internal\camera_service\web\setup.html`, its static assets, `_internal\certifi\cacert.pem`, `_internal\yolo26n.pt`, `_internal\kaggle-model\scissors_yolo11m_960.pt`, and `_internal\face_models\models\buffalo_l\{det_10g,w600k_r50}.onnx`. Confirm optional exported detectors if selected. Run the mandatory site checklist before sending an installer to paying customers.
