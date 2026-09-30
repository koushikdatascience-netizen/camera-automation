# Local Windows Camera Eye source release gates

Do not build PyInstaller or Inno Setup until every physical/manual gate below passes.
The automated tests use isolated data. The source commands below deliberately use
the existing activated ProgramData configuration, personnel, and camera database.
Local manual confirmations are audited in `person_events` as `MANUAL_*`; they do
not enqueue CRM/cloud mutations. Existing automatic attendance and cloud delivery
continue independently. Recognition alone creates an observation; Check In confirms
it. Break timestamps are persisted on the existing attendance session, and the audit
contains all manual transitions. The compact list shows the latest break pair.

## Automated validation

From PowerShell in the repository:

```powershell
Set-Location -LiteralPath 'D:\Madhushala Software\Camera ai\camera-automation'
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path $PWD 'data\test-tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
python -m py_compile camera_service/orientation.py camera_service/config.py camera_service/camera_manager.py camera_service/camera/worker.py camera_service/attendance_station.py camera_service/storage.py camera_service/api.py camera_service/launcher.py
python -m pytest tests -q
$env:CAMERA_AUTOMATION_HOME = Join-Path $PWD 'data\package-validation'
$env:CAMERA_AUTOMATION_CONFIG = Join-Path $env:CAMERA_AUTOMATION_HOME 'missing.yaml'
python test_production_package.py
node tools/browser_release_smoke.cjs
```

Browser smoke uses isolated edge/portal servers to retain the existing release
check; it does not change portal code or use customer ProgramData. It tests real
shared annotated streams and mocked recognition data for all four operator actions.
Backend tests validate real SQLite transitions, expiry, retries, and API serialization.
Range tests validate HTTP 206 with a small fixture; real MP4 playback is a manual gate.

Validation on October 1, 2026: all modified Python files compile; `python -m pytest
tests -q` passes **111 tests** with one existing Starlette/AnyIO deprecation warning;
`python test_production_package.py` passes; `node tools/browser_release_smoke.cjs`
passes with zero page errors. Desktop and mobile screenshots were visually reviewed.
No installer was built. The first sandbox browser launch was denied by the OS;
the test passed when run with browser-launch access. An intermediate Wall close
control failure was fixed and the expanded test passed on the final implementation.

## Start SOURCE with the installed configuration

First explicitly exit the installed EXE/service using its normal controls. If it
only runs in the background, stop that application through Task Manager before
running source. Do not run two workers against the same physical camera/database.
The commands refuse to proceed if 8091 is occupied; they do not terminate a process.

In PowerShell window 1:

```powershell
Set-Location -LiteralPath 'D:\Madhushala Software\Camera ai\camera-automation'
$python = if (Test-Path -LiteralPath '.venv311\Scripts\python.exe') {
    (Resolve-Path -LiteralPath '.venv311\Scripts\python.exe').Path
} else { (Get-Command python).Source }
$env:PYTHONUTF8 = '1'
$env:CAMERA_AUTOMATION_HOME = Join-Path $env:ProgramData 'MadhushalaCameraAI'
$env:CAMERA_AUTOMATION_CONFIG = Join-Path $env:CAMERA_AUTOMATION_HOME 'config.yaml'
if (-not (Test-Path -LiteralPath $env:CAMERA_AUTOMATION_CONFIG)) { throw 'Installed runtime config.yaml was not found.' }
if (Get-NetTCPConnection -LocalPort 8091 -State Listen -ErrorAction SilentlyContinue) { throw 'Exit the existing service before testing SOURCE.' }
$env:AUTO_OPEN_BROWSER = '1'
& $python -m camera_service.launcher --host 127.0.0.1 --port 8091
```

Keep window 1 running. In PowerShell window 2:

```powershell
Set-Location -LiteralPath 'D:\Madhushala Software\Camera ai\camera-automation'
$python = if (Test-Path -LiteralPath '.venv311\Scripts\python.exe') {
    (Resolve-Path -LiteralPath '.venv311\Scripts\python.exe').Path
} else { (Get-Command python).Source }
$base = 'http://127.0.0.1:8091'
Invoke-RestMethod "$base/health" | Select-Object status, runtime
Invoke-RestMethod "$base/ready"
Start-Process "$base/setup"
$cameraId = 'webcam_1'
$cameraPath = [uri]::EscapeDataString($cameraId)
Invoke-RestMethod "$base/api/v1/cameras/$cameraPath" | Select-Object camera_id, rotation_degrees, camera_role, enabled, features
```

Confirm license remains active, cloud sync remains healthy, and the reported model
runtime remains OpenVINO when that is the installed selection. Do not use activation
bypasses or alter inference backends to get a gate to pass.

## Required physical/UI checks

1. In **Camera Setup**, edit webcam_1, select **Rotation: 180°**, save. Other
   cameras remain at their configured orientation; there is no webcam hardcode.
2. Open Live View and verify the webcam image is upright after explicitly opening it.
3. Verify YOLO/face boxes align with the upright person/face.
4. Click Capture and verify the saved snapshot is upright. Check a new unknown or
   recognized evidence snapshot as well.
5. Trigger a new unknown incident, wait for clip completion, and verify its MP4 is
   upright. Previously generated evidence keeps its original orientation.
6. Close Live View, leave the page, then reopen it. In browser DevTools Network,
   clear the log and filter `stream`: there must be no camera stream request until
   a camera is explicitly opened. Camera status/AI counters still advance.
7. Click **Open Live View** on one camera. There must be one selected preview stream.
8. Verify the existing annotated feed, and switch cameras. The previous preview
   request must close. Wall opens multiple feeds only after explicitly clicking Wall.
9. With an ENTRANCE_EXIT camera and attendance/face recognition enabled, present an
   enrolled employee. Verify snapshot, name, employee code, confidence, and timestamp.
   Leaving the view or losing recognition clears the candidate; it expires after
   15 seconds without a fresh recognition. An empty panel has no attendance actions.
10. Verify OUT offers Check In; IN offers Check Out/Start Break; ON_BREAK offers
    End Break/Check Out. Click through the transitions and try a rapid double click.
    A duplicate action must not create another attendance record. A stale state or
    expired candidate is rejected and refreshed. An automatic confirmed entrance can
    already be IN; automatic observations are preserved.
11. Verify the compact recent list shows the employee code, snapshot when available,
    entry/exit, latest break pair, and duration/status. Check local audit records.
12. In Overview Alerts and Unknown Persons, verify Snapshot works using the local API.
13. A completed unknown/security clip shows Video. An incident without a completed
    clip has no broken Video action. Refresh Overview after a recording completes.
14. Click Video. Verify HTML5 playback and seeking; close the modal and confirm the
    player releases its source. Merely displaying alerts must not fetch MP4s.
15. Close the preview. Verify AI counters continue advancing, using the commands below.
16. Press Ctrl+C in source window 1, wait for shutdown, then rerun its launch command.
    Verify camera rotation remains 180°, the frame remains upright, and license/sync,
    face recognition, RTSP cameras, snapshots, and new clips continue working.
17. While source is running, launch source again using the commands below. The
    background second launch exits 0 without another Uvicorn bind/start; foreground
    launch opens the existing setup URL. The original process remains healthy.

Useful checks in window 2 (after the UI save/actions):

```powershell
Invoke-RestMethod "$base/api/v1/cameras/$cameraPath" | Select-Object camera_id, rotation_degrees
Invoke-RestMethod "$base/api/v1/cameras/$cameraPath/attendance-station" | ConvertTo-Json -Depth 6
(Invoke-RestMethod "$base/api/v1/person-events").items |
    Where-Object event_type -Like 'MANUAL_*' |
    Select-Object -First 12 person_id, camera_id, event_type, event_time, metadata_json
Start-Process "$base/api/v1/cameras/$cameraPath/snapshot"
$before = Invoke-RestMethod "$base/api/v1/cameras/$cameraPath/status"
Start-Sleep -Seconds 5
$after = Invoke-RestMethod "$base/api/v1/cameras/$cameraPath/status"
$before, $after | Select-Object online, frames_received, capture_fps, ai_fps
$clip = (Invoke-RestMethod "$base/api/v1/unknown-incidents?limit=50").items |
    Where-Object { $_.camera_id -eq $cameraId -and $_.clip_available } |
    Select-Object -First 1
if ($clip) {
    curl.exe -s -D - -o NUL -H 'Range: bytes=0-1023' "$base$($clip.clip_url)"
} else { Write-Host 'No completed unknown clip yet; trigger and wait for a new incident.' }
& $python -m camera_service.launcher --host 127.0.0.1 --port 8091 --background
Write-Host "Second background launch exit code: $LASTEXITCODE"
& $python -m camera_service.launcher --host 127.0.0.1 --port 8091
Invoke-RestMethod "$base/health" | Select-Object status, runtime
```

The range response should be `206 Partial Content` with Content-Range. To restart
source, use Ctrl+C in window 1 and rerun its launcher command; after restart rerun
the rotation/status queries. No installer build is authorized by this checklist.

## Changes and remaining release limits

- Persisted rotation: `camera_manager.py`, `config.py`, `orientation.py`,
  `camera/worker.py`, camera create/update API, Camera Setup selector.
- Local attendance: `attendance_station.py`, additive session columns in
  `storage.py`, station/action/snapshot API, `web/static/station.js` and `setup.html`.
- Evidence: public availability flags/local URLs with path confinement; existing
  encoder/storage clip finalization and FileResponse range handling retained.
- Startup: healthy-service check and Windows mutex for source and frozen launches.
- Validation: orientation, station, evidence, launcher tests plus extended browser smoke.

Manual break state survives restarts. Recognition candidates do not: fresh AI
recognition is required after restart. Recent sessions show the latest break pair;
the local audit preserves every manual break event. A person switching between
cameras/actions can receive a state-conflict response and must refresh before retry.
Physical webcam/RTSP behavior, real enrollment, evidence orientation, actual codec
playback, and signed licensing/cloud sync on the installed machine require the
manual gates above. No cloud portal source, video encoder, or installer was changed.

Files changed:

```text
camera_service/api.py
camera_service/attendance_station.py
camera_service/camera/worker.py
camera_service/camera_manager.py
camera_service/config.py
camera_service/launcher.py
camera_service/orientation.py
camera_service/storage.py
camera_service/web/setup.html
camera_service/web/static/station.js
tests/test_attendance_station.py
tests/test_camera_orientation.py
tests/test_launcher_instance.py
tests/test_local_evidence.py
tools/browser_release_smoke.cjs
docs/LOCAL_WINDOWS_SOURCE_RELEASE.md
```
