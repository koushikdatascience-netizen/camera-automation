# Camera Eye Pilot Release Work

## Status: BLOCKED for customer installation

Source and browser validation passed, but no newly built/installed package or physical camera deployment has been validated. The exact retrained scissors weights are absent at `kaggle-model/scissors_yolo11m_960.pt`. The available `jewellery_tag_best.pt` contains only the `jewellery_tag` class. Do not substitute it. This PR must not be merged or deployed as a production installer yet.

## Changes

- Edge: one persistent saved-camera capture/AI pipeline shared by raw/annotated viewers; stable per-camera ByteTrack, stale-status handling, bounded RTSP reconnect, face failure isolation, local CPU fallback, crossing-time evidence, and license-aware worker limits.
- Security: production shop-scoped activation and trusted account provisioning, strict machine-bound signed cache, loopback-only local API with origin/host checks, role checks, shop-scoped SQL events and evidence, masked sources, no local evidence paths in cloud event payloads.
- Cloud/UX: canonical settings and correct tracking_mode contract, edit/save/delete propagation, real Alerts/evidence UI, live route availability, hidden stream cleanup, and repaired local inline script syntax.
- Packaging: deterministic asset paths, fail-fast required scissors/face files, bundled certifi/exported model files, frozen single-instance mutex, logon auto-start, non-destructive customer ProgramData policy.
- Docs: installation, CRM handoff, troubleshooting and the complete pilot release gate.

## Tests

- `python -m pytest tests -q`: **87 passed**, one Starlette deprecation warning.
- `python test_production_package.py`: source health/ready/setup/CRUD/invalid RTSP/controls/attendance/incident smoke passed. Not an installed EXE test.
- `node tools/browser_release_smoke.cjs`: desktop/mobile edge live video, overlay toggle, offline tile, cleanup on navigation, portal Alerts; zero browser page errors.
- `python tools/smoke_shared_runtime.py`: two independent file-camera workers ONLINE, one pipeline per camera, shared viewers.
- `python tools/benchmark_video.py test.mp4 --model yolo26n.pt --frames 30 --stride 3 --tracking --imgsz 384`: 30 detections with track ID 1 present in all 30 sampled frames; no failures; warm p50 about 30 ms. This short sample does not establish seven-camera client performance.
- OpenVINO video sample: 15 inferred frames, 16 detections, no failures; cold start still substantial.

## Required Next Steps

1. Supply the exact retrained scissors model. Build a fresh installer using `docs/EDGE_INSTALLATION.md`; inspect contents and checksum. Do not reuse the existing old `dist/installer` file.
2. Run the one-site manual checklist in `docs/PRODUCTION_RELEASE_CHECKLIST.md` for activation, reboot, real webcam/RTSP, recognition, crossing snapshots, scissors alarm/clip, cloud evidence, network outage and upgrade preservation.
3. Validate production PostgreSQL, TLS, CRM session role/scope, one-time codes and edge event/evidence delivery. No production server was modified in this work.

## Remaining Boundaries

Cloud Live Cameras is inventory/health only: secure remote video transport is not implemented. Alert clips are local while snapshots sync. Background startup occurs at Windows logon, not before login. No evidence retention or tamper-proof/encrypted-at-rest client secrets are claimed. Review commercial model redistribution terms and rotate previously exposed real credentials manually.
