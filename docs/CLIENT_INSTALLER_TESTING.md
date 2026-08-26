# SnapKey Vision AI Client Installer Testing

Send the client only this file:

```text
SnapKeyVisionAISetup.exe
```

Do not send the Git repository, Python commands, or source folder.

## Client Machine Steps

1. Double-click `SnapKeyVisionAISetup.exe`.
2. Install with default options.
3. Open **SnapKey Vision AI** from desktop or Start Menu.
4. Browser should open:

```text
http://127.0.0.1:8091/setup
```

If the browser does not open automatically, open the link manually.

## Webcam Test

In **Cameras**, add:

```text
Camera ID: client_webcam
Name: Client Webcam
Source: 0
```

If `0` fails, try:

```text
1
```

For low-end client laptops:

```text
Tracking FPS: 1 or 2
Image Size: 320 or 384
Quality: 50-60
Mode: detect
```

Enable:

```text
Shoplifting
```

Open **Tracking** and show scissors to the webcam.

Expected:

```text
Live video keeps moving
Bounding box appears
Label shows scissors
Beep alert plays
Snapshot is saved
Clip is saved
Alert appears in Security Alerts
```

## Where Client Data Is Stored

```text
C:\ProgramData\SnapKeyVisionAI
```

This contains local database, evidence, snapshots, and clips.

## Build Team Steps

On internal build PC:

```powershell
.\packaging\windows\build_installer.ps1
```

Installer output:

```text
dist\installer\SnapKeyVisionAISetup.exe
```
