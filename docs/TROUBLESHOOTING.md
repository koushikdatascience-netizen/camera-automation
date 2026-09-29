# Camera Eye Troubleshooting

Start with GET http://127.0.0.1:8091/health, /ready and /api/v1/edge/status. Health is API liveness; a camera being offline does not make the entire API unhealthy. Read camera last_frame_at, state, capture/AI FPS, reconnect_count and safe last_error. Active backend diagnostics reflect loaded models; configured model preference alone is not proof that inference succeeded.

| Symptom | Check / Action |
| --- | --- |
| Setup does not load / failed to fetch | Confirm EXE process and port. Rebuilt setup HTML/static files must match the executable. Do not immediately stop the process after starting it. |
| Port 8091 occupied | Get-NetTCPConnection -LocalPort 8091; identify OwningProcess. Stop only the obsolete Camera Eye instance, not unrelated services/Python. |
| Camera offline | Verify LAN, power, webcam index, camera source and enabled state. Check current frame timestamp. Worker retries with bounded backoff; a broken camera must not prevent other cameras. |
| RTSP authentication failure | Verify URL/credentials through Camera Setup. Stored passwords are masked; blank/preserved sources must not be replaced with asterisks. Use RTSP port 554, not device management port 8000 unless vendor configured otherwise. |
| Camera test works but no boxes | Enable tracking mode track, inspect AI FPS/model backend, and show an actual supported object/person. Face initialization can lag without stopping person tracking. |
| IDs missing | detect mode intentionally has no IDs. track requests ByteTrack persist=True. Do not manufacture IDs from detection order. Occlusion/reconnect can legitimately change IDs. |
| Face service unavailable | Verify bundled buffalo_l detector/recognizer, enrollment and face quality. Haar fallback can detect a face but cannot identify it. Do not treat missing embeddings as an unknown-person alarm. |
| Model fallback | CPU selection prefers available OpenVINO/ONNX/PT. Exported runtime errors retry local artifacts; diagnostics show actual fallback. A slow cold start is not sustained FPS. Explicit override should point to a local existing file. |
| Cloud unavailable / TLS failure | Inspect local sync status and certificate bundle. Never disable TLS verification. Local cameras should keep running and outbox retries resume after connectivity returns. |
| Edge offline in cloud | Verify recent heartbeat, signed license, shop scope and scoped edge credential. Portal online state is heartbeat freshness, not a guarantee of remote video. |
| Activation rejected | Production requires an unexpired shop-scoped one-time portal code. Global shared codes no longer onboard production edges. PostgreSQL and matching server signing/public keys are required. |
| License invalid / expired | Inspect license reason and grace expiry. Corrupt cache enters limited mode. Re-activate through authorized portal; never alter signed cache contents or bypass the gate. |
| License belongs to another machine | Generate a new authorized activation for that PC. Copying ProgramData/license cache from another PC is not a valid activation. |
| Attendance missing | Use ENTRANCE_EXIT + attendance + recognition + tracking; enroll person and calibrate gate direction/position. Inspect confirmed entry/exit events, not disappearance from a frame. |
| Break not automatically recorded | Intended: tracker loss is not proof of a break. Use confirmed break workflow and verified CRM mapping. |
| Alerts not in cloud | Inspect outbox retry errors, license/scope, evidence file existence and heartbeat. Cloud Alerts only displays real synced shop events. Native sound happens on edge PC, not automatically on a remote phone. |
| Installer cannot replace files | Close Camera Eye before retrying. Do not ignore replacement errors or delete ProgramData. Reinstall/upgrade preserves persistent data. |
| Monitoring absent after reboot | Installer startup is at Windows logon. Confirm a user is logged in and the EXE starts once. Unattended pre-logon Windows service operation is not certified. |

Windowed EXE logs: `C:\ProgramData\MadhushalaCameraAI\logs\launcher.log` (or CAMERA_AUTOMATION_HOME\logs\launcher.log). Customer data normally uses ProgramData\MadhushalaCameraAI\data. Copy only necessary redacted diagnostics for support; never share activation codes, tokens, private keys, unmasked RTSP URLs or face embeddings publicly.

Back up ProgramData before repairs/upgrades. This release adds no destructive cleanup/retention policy. Check disk space and queue size during a pilot soak.
