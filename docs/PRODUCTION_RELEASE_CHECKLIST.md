# Camera Eye Pilot Release Gate

Branch: `feat/camera-eye-live-view-ux`. PR: https://github.com/koushikdatascience-netizen/camera-automation/pull/2

## Current Decision: BLOCKED Pending Fresh Installer And Site Validation

Source validation is not installer validation. Do not ship an old installer as this release.
No production deployment or merge is authorized by this checklist.
The build machine now contains `kaggle-model/scissors_yolo11m_960.pt`; loading it confirmed the `scissors` class. The installer built before the latest operator UI changes is stale and must be rebuilt and installed for the pilot. A different `jewellery_tag_best.pt` exists and its only trained class is `jewellery_tag`, not scissors.

## Audit And Fixes

| Severity | Finding | Resolution |
| --- | --- | --- |
| BLOCKER | Local setup script contained a literal backslash-n outside a string | Corrected; inline scripts compile and browser smoke runs |
| BLOCKER | Browser viewers and background workers opened separate capture/inference pipelines | Saved cameras own one shared pipeline; viewers consume cached raw/annotated JPEGs |
| HIGH | Unchanged cloud assignments repeatedly changed configuration versions | Compare normalized configuration before writing; stable workers on repeated polling |
| HIGH | Track IDs/model state could leak across cameras | Camera-scoped model/backend caches; no fabricated per-frame IDs; cleanup after pipeline termination |
| HIGH | Start/stop reported probe results rather than controlling monitoring | Persist enabled state and reconcile actual workers |
| HIGH | Camera probe could hang on unreachable RTSP | FFmpeg open/read timeouts; HTTP smoke checks have timeouts |
| HIGH | Face failures could prevent tracking/startup | Lazy initialization; runtime face failure isolation; degraded availability |
| HIGH | Cloud sent textual JPEG quality; edit did not restore settings | Normalize settings; numeric quality; edit restores flags/mode and preserves disabled state/source |
| HIGH | Cloud delete did not stop edge camera | Scoped durable CAMERA_DELETE command; expired command claims can retry |
| HIGH | Shop filtering after LIMIT hid valid events | SQL shop filter before LIMIT in SQLite and PostgreSQL |
| HIGH | Global code allowed cross-shop owner registration/onboarding | Production registration requires trusted backend key; production activation requires scoped one-time code |
| HIGH | Unlicensed frozen configuration could use demo mode | Frozen app requires signed license even if local activation flag is changed |
| HIGH | Cache corruption could raise instead of entering limited mode | Safe limited status; atomic cache replacement; strict machine binding |
| HIGH | Installer omitted optional assets/models or runtime certificates | Absolute spec roots, required scissors/face assets, optional exported detectors, bundled certifi |
| HIGH | Windowed launcher imported API before log stream setup | Streams initialized before API import; frozen single-instance mutex |
| HIGH | Alarm event throttling caused unknown alarm gaps | Camera-scoped alarm renewals are independent of event cooldown |
| HIGH | Attendance evidence could predate the crossing | Crossing event stores current frame; engine uses crossing snapshot |
| MEDIUM | Cloud live navigation pointed to an unserved page | Explicit live page whitelist; no claim of remote video transport |
| MEDIUM | Local hidden live images retained streams | Remove image sources and refresh timer on leaving Live Cameras |

## Automated Evidence

- Baseline suite: 65 passed, 6 failed. Six old cloud fixtures did not match current canonical shop/session and protected licensing contracts.
- Updated suite: 87 passed, one deprecation warning; focused tests cover sharing, reconnect isolation, stale status, quality normalization, deletion, host/origin restrictions, license cache/grace/machine checks, production registration/activation, scoped evidence, crossing snapshots and exported-model fallback.
- Source package smoke: health, readiness, setup HTML, CRUD, bounded invalid RTSP, controls, personnel, attendance and incidents passed.
- Actual YOLO26n/PT + ByteTrack: 30 sampled test.mp4 frames, 30 detections, track ID 1 in all 30 frames, zero failures. A warm sample run measured p50 30.175 ms and p95 33.153 ms, 7.113 effective inference FPS including decode/start overhead. Not a client sizing guarantee.
- OpenVINO sample: 15 inferred frames, 16 detections, zero failures, p50 34.5 ms. Cold start materially increased total runtime.
- Two real file-camera workers: one pipeline each, both ONLINE, roughly 9.8 capture FPS and 0.8 AI FPS during short startup-inclusive sampling. Two viewers closing did not restart AI. Seven-camera or long-duration performance is NOT validated.
- Playwright desktop/mobile: actual local video rendering, overlay toggle, navigation releasing hidden streams, offline tile isolation and cloud Alerts empty state; zero page errors.
- JavaScript: eight inline scripts compile; main.js syntax check passes.

## Mandatory Manual Gate: One Site Checklist

1. Build the new installer using EDGE_INSTALLATION.md. Check bundled detector, scissors model, face models, web assets and certifi. Record installer SHA-256 and version.
2. On a pilot PC, back up existing ProgramData, close only this application, install without deleting customer data. Confirm config, database, license and evidence remain.
3. Launch once, verify `/health`, `/ready` and `/setup` on 127.0.0.1:8091. Generate a scoped activation code from the authenticated portal, activate, verify signed license and successful heartbeat.
4. Save a webcam/RTSP entrance camera with attendance, face recognition and tracking enabled. Enroll a real test person. Show moving boxes and stable IDs; verify every actual entry/exit crossing has correct time and its own current snapshot. Calibrate line/direction first.
5. Add a general/security camera; confirm known person is not unknown. A clear unknown face should create an alarm/event/snapshot. Test scissors detection, continuous local alarm and locally recorded clip, then remove the object and confirm the alarm expires.
6. Open two live tabs, switch AI overlay, use 1/4/9 layouts/fullscreen. Close all tabs for five minutes and verify monitoring/attendance continues.
7. Disconnect one camera and internet separately. Other cameras/local AI must continue; reconnect and verify recovery, queued events and snapshots arrive in the correct cloud shop once only. Test cloud settings and deletion against the actual edge.
8. Reboot, log in to Windows, confirm auto-start and a single EXE process. Repeat upgrade, verify customer data survives. Run a 30-minute soak on the lowest supported PC with the sold camera count.

Do not change BLOCKED to READY FOR PILOT until these outcomes are recorded.

## Known Boundaries

- Cloud Live Cameras is inventory/health only. Secure remote WebRTC/relay transport is not implemented. Never expose LAN RTSP/554 publicly.
- MP4 alert clips remain local; cloud evidence synchronization currently uploads images, not clip files.
- Auto-start is Windows logon startup, not a Windows service. Monitoring while nobody is logged in is not certified.
- Production PostgreSQL/TLS/activation/evidence delivery requires site validation; SQLite tests are not a substitute.
- Config/SQLite camera credentials remain readable to authorized local filesystem users/admins. Restrict Windows access and cloud backups; this release does not claim encrypted-at-rest credentials or tamper-proof offline licensing. Local administrator access can defeat local-only controls.
- No automatic evidence/outbox retention policy is added. Monitor disk capacity; choose retention/legal policy before sustained rollout. Do not silently discard unsynced attendance/alerts.
- Breaks are explicit confirmed business actions, not inferred from disappearance. CRM API delivery still requires verified mappings and credentials.
- Face recognition depends on enrollment, lighting/pose and offline models. Demographic data is not fabricated. Review redistribution/commercial model licenses before external distribution.
- Native sound must be tested on the actual Windows speaker/audio session. Detection of scissors alone is an object-security rule, not proof of shoplifting.

## Next Release: Signed Updates

Design `GET /edge/v1/update`, `POST /edge/v1/update-status` and authenticated package download with version, SHA-256, signature and channel. Use separate UpdateChecker, UpdateDownloader, SignatureVerifier and CameraEyeUpdater.exe. Download/verify first, stop only Camera Eye, back up binaries, replace application files only, preserve ProgramData, restart and health-check, roll back binaries if unhealthy. Disabled until an audited signing/rollback path exists; no self-overwrite implementation in this pilot.
