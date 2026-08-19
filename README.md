# Camera Automation — Production P0 Attendance + Face Recognition

This package implements the missing end-to-end attendance vertical slice: camera/replay input, production ByteTrack adapter, face recognition with InsightFace, multi-frame identity consensus, directional entry/exit, attendance sessions, unknown-person evidence, SQLite persistence, and FastAPI management endpoints.

## Windows quick start
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
python -m camera_service
```
Open `http://127.0.0.1:8090/docs`.

## Client RTSP
Use Hikvision substreams for AI, e.g. `/Streaming/Channels/102`. Keep credentials only in local `config.yaml`; do not commit them.

## Important production validation
Before client sign-off, calibrate the face threshold on real CCTV footage, draw the entrance line from a real frame, verify entry direction, benchmark all 7 streams on the client PC/GPU, and verify InsightFace/ONNX/Ultralytics versions on that machine.
