# Camera Automation

Production demo system for camera setup, live preview, YOLO object tracking, face enrollment, known-person recognition, attendance presence, break events, and unknown-person alerts.

## Quick Start

```powershell
cd "C:\path\to\camera-automation"
python -m venv .venv311
.\.venv311\Scripts\python.exe -m pip install --upgrade pip
.\.venv311\Scripts\python.exe -m pip install -r requirements.txt
$env:PORT="8091"
.\.venv311\Scripts\python.exe -m camera_service.launcher
```

Open:

```text
http://127.0.0.1:8091/setup
```

If the browser shows an old screen, press `Ctrl + F5`.

## Camera Sources

Use one of these in **Camera Source**:

```text
rtsp://USER:PASS@CAMERA_IP:554/Streaming/Channels/102
1
dshow:USB Camera
```

For Hikvision cameras, prefer the substream `/Streaming/Channels/102` for smoother AI processing.

For Windows USB camera testing, use:

```text
dshow:USB Camera
```

## Demo Flow

1. Go to **Cameras**.
2. Add camera and click **TEST CONNECTION**.
3. Save camera.
4. Use **LIVE** for smooth raw stream.
5. Use **TRACKING** for YOLO boxes, track IDs, and known-face labels.
6. Go to **Personnel**.
7. Add person with name, employee code, role, and a clear face image.
8. Open **TRACKING** and stand close enough for the face to be visible.
9. Go to **Attendance** to see presence and break/person events.
10. Go to **Unknown Persons** or watch the top alert banner for unidentified-person incidents.

## Personnel And Face Enrollment

Use a clear front-facing image with exactly one usable face.

Good enrollment image:

```text
Bright lighting
Face looking at camera
Face is not tiny
No blur
Only one person in the image
```

If enrollment fails, capture a closer face image and upload again.

## Attendance Behavior

The system has two separate concepts:

```text
Attendance entry/exit: requires line-crossing camera configuration.
Break / removed from view: starts when a recognized person disappears from camera view.
```

When a recognized worker leaves the camera view:

```text
BREAK_START is stored with name, time, and camera.
```

When the worker returns and is recognized again:

```text
BREAK_END is stored with name, time, and camera.
```

These records are visible in **Attendance -> Break / Person Events**.

## Unknown Alerts

If unknown detection is enabled and an unrecognized person is confirmed, the UI shows a red alert banner and lists incidents in **Unknown Persons**.

Open incidents can be acknowledged from the UI.

## Common Fixes

Port already busy:

```powershell
$env:PORT="8092"
.\.venv311\Scripts\python.exe -m camera_service.launcher
```

USB camera locked by another app:

```powershell
ffmpeg -hide_banner -y -f dshow -i video="USB Camera" -frames:v 1 -update 1 camera_test_frame.jpg
```

If FFmpeg says the device is already in use, close browser camera tabs, Teams/Zoom/OBS/Camera app, unplug/replug the USB camera, or restart Windows.

Push latest code:

```powershell
git push origin main
```
