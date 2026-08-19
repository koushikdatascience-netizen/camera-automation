# Client Validation Checklist

1. Install dependencies in a fresh Windows venv.
2. Copy `config.example.yaml` to `config.yaml` and add local Hikvision RTSP substreams.
3. Keep RTSP credentials local; never commit `config.yaml`.
4. Start one entrance camera first and confirm frame decoding.
5. Draw the attendance line from a real entrance frame and verify `inside_side` by walking both directions.
6. Enroll 3-5 good images for each OWNER/MANAGER/WORKER through `/docs`.
7. Calibrate `known_threshold` against real CCTV samples before enabling unknown-person alerts.
8. Verify arrival once, temporary occlusion (no exit), cross-camera presence (no duplicate arrival), and confirmed directional exit.
9. Verify an unenrolled person creates one unknown incident/evidence record rather than repeated incidents.
10. Add all seven substreams and benchmark CPU/GPU, capture FPS, inference FPS, and reconnect behavior.

## Known P0 limitations
- The ZIP is a corrected production-P0 reconstruction created from the audited public repository structure; it is not a byte-for-byte clone because the environment could not download the GitHub archive directly.
- Evidence currently saves unknown person/full-frame snapshots. The existing project's pre/post video clip recorder should be merged if you require 5s-before + 10-15s-after clips immediately.
- Ultralytics ByteTrack and InsightFace require their runtime packages/models on the client machine. The code has a deterministic fallback path for development/tests, but client sign-off must use the production dependencies.
- Face thresholds and line geometry are intentionally configuration values and must be calibrated at the shop.
