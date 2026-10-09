# CRM enrollment and attendance hardening — 9 October 2026

Base: `d88d170`, branch `codex/crm-enrollment-cache-20261009`.

## Confirmed causes and boundaries

- `_refresh_crm_personnel_locked()` decoded images and ran enrollment inside the personnel request path. Its process-local fingerprint set was lost on restart, so unchanged images were inferred again. Directory/edge polling TTLs already existed and are retained.
- A 512-element CRM vector was accepted without model provenance. Dimension alone does not prove InsightFace compatibility. The new mirror uses image-derived templates only.
- Raw JPEG Base64 begins `/9j/`; treating any leading slash as a path discarded valid images and prevented enrolled-image authentication.
- `_crm_face_token()` permitted current camera evidence to replace CRM enrollment images and concurrent cache misses could perform repeated authentication. Both are corrected while retaining the existing token vault and CRM endpoints.
- PostgreSQL personnel upsert reassigned conflicting IDs to another tenant/shop. Conflict updates now require matching ownership; new mirrored IDs include tenant/shop/user scope.
- Manual actions copied unfinished recognition evidence without a completion link. Shared clip capture and completion ordering compounded the loss. All four actions now receive finalized recognition evidence in both local history and their pending delivery payload.

The historical ~793% VPS CPU reading was **not reproduced or attributed**. No VPS or client installation was modified.

## Enrollment cache and worker

Valid embeddings remain in `cloud_face_profiles`, keyed by `crm-image:<SHA256>`. The hash includes Camera Eye tenant code, CRM tenant UUID, shop, CRM user, local person, decoded-image SHA256, deployed detection/recognition weight hashes, detector size, provider and enrollment rules/version. No raw enrollment image is persisted. Fingerprints and templates are sensitive and must remain in protected database volumes/backups.

New additive table `crm_enrollment_rejections` stores scoped cache keys and generic rejection reasons only. Invalid/ambiguous templates are not represented by arbitrary or zero vectors. This prevents repeat inference for rejected unchanged images, including after restart. Changed images/model keys are reevaluated. No existing attendance/evidence table is rebuilt or emptied.

Refresh jobs are bounded and single-flight per tenant/shop. Default concurrency is one; image processing uses the shared locked model. PostgreSQL advisory transaction locks prevent duplicate refresh processing across portal processes and release on interruption. SQLite development uses process-local single-flight; use one portal process with SQLite. Completed/rejected cache entries survive restart; unfinished jobs are rediscovered on polling. Jobs retry on later polling with exponential backoff, capped at 300 seconds. Shutdown finishes active work and cancels queued jobs.

Requests return the existing mirror immediately. First initialization with no mirror returns HTTP 503, avoiding an authoritative empty snapshot that would erase the edge roster. Malformed responses, inconsistent tenants/shops, invalid image encodings and CRM transport failures retain existing templates. Explicit template removal or deactivation invalidates managed templates; omitted image fields do not imply revocation. Intentional unmapped local people remain active. Legacy templates without model/image provenance are withdrawn during successful refresh; operators must provide verified enrollment images to restore them.

Internal counters: cache hits, rejected-cache hits, inference/model initialization counts; worker status: queue depth, completed/failed jobs and processing seconds. They expose no images, embeddings or credentials.

## Authentication and attendance

```mermaid
flowchart LR
  A[Public tenant face directory] --> B[Bounded background enrollment]
  B --> C[Persistent model-scoped templates]
  C --> D[Scoped edge personnel sync]
  D --> E[Fresh local InsightFace recognition]
  E --> F[AUTO policy or operator confirmation]
  F --> G[Atomic local attendance and queue]
  G --> H[Evidence finalization and cloud ingestion]
  H --> I[Delivery claim and predecessor ordering]
  I --> J[Employee token cache]
  A --> J
  J --> K[CRM Face Login using enrolled image when renewal needed]
  K --> L[Confirmed CRM attendance operation]
  J --> L
  L --> M[Delivery receipt or retry/reconciliation]
```

| Operation | Endpoint | Authentication |
|---|---|---|
| Directory | GET `/api/User/face-embeddings/{tenantCode}` | No legacy static JWT |
| Employee authentication | POST `/api/Auth/loginUsingFaceTenant` | `{base64Image: enrolled CRM image, tenantId: CRM tenant UUID}` |
| Check-in / normal checkout | POST `/api/UserRoster/LoginLogout` | Same employee Face Login token; existing date/start/off payload preserved |
| Break start | POST `/api/UserBreak/start-break` | Employee token; existing CRM break-master mapping required |
| Break end | POST `/api/UserBreak/end-break` | Same employee token |
| Qualifying absence | POST `/api/UserActivity/auto-logout` | Employee token; existing endpoint-specific contract and feature flag preserved |
| Monthly roster | Existing `/api/UserRoster/GetUsersRoster` | Separate verified tenant service credential where required |

The client preserves the confirmed existing header contract: the returned Face Login token is forwarded verbatim in `Authorization`. Live CRM staging must confirm header formatting.

Tokens use the existing Fernet vault, now also bound to the CRM tenant UUID. Old envelopes lacking that UUID are renewed by Face Login before reuse. Expiry remains the earlier of 24 hours and JWT `exp`, with five-minute renewal margin. JWT parsing is an expiry hint, not signature verification. Identity/tenant/business success are checked against the trusted HTTPS CRM response. Whitespace/non-string/missing credentials are rejected. Per-employee locks and per-tenant directory fetch locks prevent concurrent refreshes within a process. CRM 401 invalidates only that employee; attendance is not replayed by token renewal. Expired unattended tokens renew using the enrolled CRM image, never another user's token or a fabricated refresh endpoint. Authentication membership lookup disallows stale-on-error directory fallback; roster display retains stale data during outages.

Manual/AUTO rules, presence grace/max-logoff policies, break mapping, working-time calculations, receipt idempotency and uncertain-result reconciliation remain in their existing services. Both production auto-logout flags stay disabled. No real CRM attendance mutations were executed.

## Live recognition and evidence

Existing per-track cooldown and latest-frame capture/AI slots are retained. Matching is vectorized, rejects malformed/nonfinite/zero vectors, and treats near-identical matches for different employees as inconclusive. Ambiguity raises a safe recognition failure so it cannot become a confirmed unknown alert. Shared model acquisition is bounded at 250 ms; overloaded cameras retry on a fresh frame instead of accumulating inference jobs. Optional `SNAPKEY_FACE_MAX_FPS` limits shared inference; default `0` preserves existing timing. Do not enable an arbitrary cap without multi-camera staging validation.

Attendance captures use independent short-clip buffers, unique filenames and completion propagation. New clips are VP8 WebM and are decoded before being marked available. Historical MP4 MIME/range handling remains. Incomplete/interrupted capture remains explicitly partial/unavailable. No frames are fabricated. Actual post-fix physical-camera acceptance remains required; synthetic tests are not evidence of successful live-camera capture.

## Local verification and benchmark

See the final recorded test counts below. All test identities/CRM mutations are mocked; local image/SQLite/video operations are real. PostgreSQL tests require an explicitly named test database and disposable schema. Current Docker Desktop Linux engine is unavailable, and no isolated DSN is configured; PostgreSQL execution is blocked, not passed.

Synthetic benchmark artifact: `artifacts/enrollment-benchmark-ca3c8231/results.json`.

| Measurement | d88d170 baseline | Persistent cache |
|---|---:|---:|
| Enrollment inferences | 12 | 4 |
| Mock model initializations | 3 | 1 |
| Process CPU seconds | 0.2812 | 0.2812 |
| Total wall seconds | 7.3456 | 6.3619 |
| Median refresh latency ms | 1178.987 | 1013.16 |
| Median isolated HTTP request latency ms | 897.361 | 307.004 |

Four synthetic users, six refreshes and three simulated restarts; real SQLite/image decoding and mocked 5 ms inference. Cache-hit ratio: 83.3%. These timings include local disk/test-environment overhead, exclude real ONNX/VPS CPU and do not establish production performance. Reproduce using `python -m tools.benchmark_crm_enrollment_cache`.

## Configuration and staging gates

- `SNAPKEY_ENROLLMENT_WORKERS=1` (supported 1–4); `SNAPKEY_ENROLLMENT_QUEUE_SIZE=32`.
- `SNAPKEY_FACE_MAX_FPS=0`; existing `SNAPKEY_CRM_PERSONNEL_REFRESH_SECONDS` and `SNAPKEY_EDGE_PERSONNEL_SYNC_SECONDS` retain their defaults.
- Existing `CAMERA_EYE_TOKEN_ENCRYPTION_KEY` is required for the vault. Preserve/backup its key securely; never put it in Git or logs. Do not replace it during an ordinary deployment.
- Keep `SNAPKEY_CRM_AUTO_LOGOUT_ENABLED=0` and `CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false`.
- Supply matching buffalo_l model files on cloud and edge. Restart after intentional model-file changes; weight hashing is cached per process.
- Restrict database/biometric volume and backup access to the service account; use encrypted disks/backups. Existing biometric tables are not additionally encrypted by this change.

Before release: run the PostgreSQL contracts, verify the actual tenant directory schema and shop-membership contract, verify enrolled-image login with a CRM-authorized test employee, exercise all four attendance actions in staging, and repeat physical-camera evidence playback. Do not use live employee records. Multi-process token renewal is locally single-flight per process; receipt/action locks remain the existing cross-process attendance safeguard.

### PostgreSQL test commands — local machine only

Start Docker Desktop explicitly before these commands. Use the repository's existing `integration-test` profile under a distinct project name. Compose requires dummy `POSTGRES_PASSWORD` and `SNAPKEY_CRM_INTEGRATION_KEY` values for interpolation even when only `postgres-test` is selected; use local test placeholders, never production secrets.

```powershell
$env:POSTGRES_PASSWORD='unused-local-test-placeholder'
$env:SNAPKEY_CRM_INTEGRATION_KEY='unused-local-test-placeholder'
docker compose -p camera-eye-enrollment-tests --profile integration-test up -d postgres-test
$env:SNAPKEY_TEST_DATABASE_URL='postgresql+psycopg://camera_eye_test:camera-eye-test-only@127.0.0.1:55432/camera_eye_test'
$env:SNAPKEY_REQUIRE_POSTGRES_TESTS='1'
.\.venv311\Scripts\python.exe -m pytest -q tests/test_postgres_attendance_bridge.py tests/test_postgres_attendance_integration.py
# After verification, remove only this disposable test service:
docker compose -p camera-eye-enrollment-tests --profile integration-test rm -s -f postgres-test
```

Do not run these test commands on the production VPS. No production cleanup/history replay is part of this change.

### Deployment / rollback plan — review only

1. Approve the exact local release SHA only after the staging gates pass. Build/tag an immutable cloud image and later rebuild the Windows installer from that approved SHA; neither was built here.
2. Capture the running portal image ID and take a consistent PostgreSQL backup with `pg_dump -Fc` using the existing service-account credential channel. Secure the backup and token-encryption key independently. Preserve server-owned Compose, `.env`, database/media volumes and deployment handler.
3. Update only the existing portal service to the approved immutable image through an approved server-owned image override. Conceptual command: `docker compose --env-file <server-env> -f <server-compose> -f <approved-image-override> up -d --no-deps portal`. Never substitute repository Compose for server-owned Compose. Verify `/health` and scoped staging smoke checks before client rollout.
4. Startup adds `crm_enrollment_rejections` only; no destructive migration is needed. Maintain both auto-logout flags disabled.
5. Roll back by selecting the previously captured portal image in the same approved override and running the same `up -d --no-deps portal` command. Retain the additive rejection table; do not drop or restore attendance data automatically. Old releases use unverified CRM vectors, so authentication/template rollback requires staging/security review; a database rollback cannot undo externally delivered attendance.
6. Deploy the edge build only after explicit client approval and confirmed three-image/playable-video physical acceptance. Retain the prior installer and backup existing configuration/database through the established installer procedure. Never reset activation or attendance history.

## Recorded final validation

- Full suite: `python -u -m pytest -q tests --basetemp artifacts/crm-cache-release-verified-20261009 --tb=short`: **333 passed, 0 failed, 7 skipped** (491.17 seconds). Log: `artifacts/crm-cache-release-verified-results.txt`.
- After the final ambiguity safeguard, affected recognition/auth/cache/evidence suites: **82 passed, 0 failed** (170.63 seconds). Log: `artifacts/crm-cache-last-safety-results.txt`. This includes the additional low-similarity tie regression added after full-suite collection.
- Evidence/cloud/attendance follow-up: **54 passed, 0 failed**. Earlier recognition/authentication/PostgreSQL selection: **78 passed, 6 skipped**; the new PostgreSQL case was added after that run�s collection. Use the final full-suite count above for the current seven PostgreSQL cases.
- Final worker shutdown/backoff/unknown-tie checks: **3 passed, 0 failed**.
- `compileall` and `git diff --check` passed.
- PostgreSQL integration: **UNVERIFIED**, seven skipped contracts. No test DSN; local Docker Linux daemon unavailable. No VPS test writes attempted.
- Live CRM schema GET: two bounded attempts, both `ConnectError`; no personnel/biometric response printed, no live authentication/attendance mutation.
- Actual physical-camera retest and real ONNX/multi-camera performance: **NOT RUN**. Do not claim production ready.

## Changed files

| Files | Purpose |
|---|---|
| `cloud_portal/api.py` | Background refresh; scoped persistent cache lifecycle; enrolled-image auth; token UUID binding; portable media upload and WebM serving |
| `cloud_portal/enrollment_worker.py` | Bounded jobs, single-flight, backoff, counters, shutdown |
| `cloud_portal/storage.py`, `cloud_portal/postgres_storage.py` | Additive rejection cache, scoped upsert, PostgreSQL refresh lock |
| `cloud_portal/crm_client.py`, `cloud_portal/attendance_tokens.py` | Directory single-flight/freshness and encrypted CRM-UUID token scope |
| `camera_service/face_service.py` | Weight/config fingerprint, single-face/embedding validation, vector matching, ambiguity and shared inference controls |
| `camera_service/attendance_station.py`, `camera_service/storage.py` | Durable evidence parent links and history/queue completion propagation |
| `camera_service/camera_manager.py` | Independent attendance clips, completion ordering, unique filenames, VP8 and decode verification |
| `camera_service/api.py`, `camera_service/cloud_client.py` | WebM MIME/upload support with historical MP4 compatibility |
| `tests/test_crm_enrollment_cache.py`, `tests/test_crm_employee_tokens.py`, `tests/test_attendance_tokens.py` | Cache, lifecycle, concurrency, malformed data, auth/expiry/isolation regressions |
| `tests/test_attendance_station.py`, `tests/test_acceptance_regressions.py` | All four evidence actions, interruptions, media encoding and upload filenames |
| `tests/test_postgres_attendance_bridge.py` | PostgreSQL advisory lock and ownership guard contract (requires isolated DSN) |
| `tests/test_attendance_defect_fixes.py` | Update mocks for optional auth-directory freshness argument |
| `tools/benchmark_crm_enrollment_cache.py`, this document | Reproducible offline benchmark and release/rollback handoff |

## Local commit sequence

- `ef1b7fde664acd2fe6561f903b56b0a7148cdd5b`: persistent scoped enrollment, authentication and storage hardening.
- `08164dc59bbf87f65f9051f3f84569e89204032e`: attendance evidence finalization and playable media.
- The following documentation/benchmark commit contains this report. Use the final branch HEAD as the release reference; nothing has been pushed or deployed.
