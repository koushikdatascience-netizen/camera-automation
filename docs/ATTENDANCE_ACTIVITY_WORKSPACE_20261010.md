# Attendance and activity workspace — 2026-10-10

Branch: `codex/attendance-activity-dashboard-20261010`. Base: `7db6582df6bb4340fd96f27fdc81c8b181ed8a08`.

## Delivered behavior

Local and cloud attendance pages use the same responsive workspace: employee/name/code, date presets/custom dates, timezone, camera, state, activity and source filters; sorted, paginated sessions; filtered CSV; working/break/overtime summaries; chronological original events; evidence previews; policy editor. Filters execute on the server, and the summary/export use all matching sessions rather than just the displayed page. CSV escapes spreadsheet formulas. Legacy sessions retain unknown source/policy labels instead of invented activity events.

The shared transition contract allows OUT → check-in, IN → break-in/checkout, ON_BREAK → break-out/checkout. Checkout terminates an active break at the checkout timestamp without fabricating a separate break-out event. Recognition restores persisted break state after restart. Automatic arrival now retains its original event in the local activity ledger, atomically with the session and edge queue. Existing manual transaction/idempotency safeguards remain.

Cloud manual controls use a versioned action handler backed by the existing CRM delivery bridge. A PostgreSQL advisory lock serializes per-person manual confirmation across portal workers through CRM/local finalization. Deterministic action IDs, predecessor receipts and one session ID cover login, break-in, break-out and logout. A remote timeout after submission remains reconciliation-required; repeated requests do not blindly resubmit. Events preserve the effective employee/shop policy snapshot. Employee selection uses a new read-only directory endpoint and does not trigger CRM personnel refresh.

## API contract

Local prefix: `/api/v2/attendance`. Cloud prefix: `/portal/v2/tenants/{tenant_id}/attendance`.

| Method/path suffix | Contract |
|---|---|
| GET `/workspace` | `items`, `total`, `page`, `page_size`, `summary`, `summary_notes`, `events`, `range`, `warnings` |
| GET `/personnel` | Safe read-only `items`: `id`, `full_name`, `employee_code`; no images, vectors, tokens or directory refresh |
| GET `/export` | UTF-8 BOM CSV of **all** filtered sessions |
| GET/PUT `/policy?person_id=...` | Shop default or employee override; saving never changes execution feature flags |
| Local GET `/personnel/{person_id}/state` | Persisted state, readable label and valid actions |
| Local GET `/events/{event_id}/evidence?index=0..2`, `/clip` | Event-scoped existing media, bounded by evidence root; missing media returns 404 |
| Cloud GET `/events/{event_id}/notifications` | Existing durable outbox delivery statuses; no provider secrets or message payloads |

Query fields: `preset=today|yesterday|last7|month|custom`, `start`, `end` (ISO dates; custom requires both), `timezone` (IANA), `search`, `person_id`, `session_id`, `shop_id`, `camera_id`, `activity`, `status`, `source`, `page`, `page_size` (1–100), `sort=login|employee|worked`, `direction=asc|desc`. A custom start/end can differ by at most 366 days. `full_session=true` requires a specific session ID and includes its original timeline across midnight; opening a session clears activity/source filters for complete history. Shop scope cannot be widened by query parameters.

Cloud manual POST: `/portal/v2/tenants/{tenant_id}/attendance-station/action`, body:

```json
{"camera_id":"camera-id","edge_id":"edge-id","recognition_event_id":"fresh-recognition-id","action":"CHECK_IN","request_id":"optional-stable-client-id"}
```

Actions: `CHECK_IN`, `CHECK_OUT`, `BREAK_START`, `BREAK_END`. Recognition must be at most 15 seconds old, from the scoped entrance camera and edge, with a scoped CRM employee mapping. Existing v1 APIs are preserved; the cloud UI uses v2. Existing historical CRM sessions without a bridge session receipt require reconciliation before v2 break/checkout.

Cloud reporting roles: OWNER, ADMIN, SUPERADMIN, MANAGER, OPERATOR. Policy edits require OWNER/ADMIN/SUPERADMIN. Native tenant/shop authorization applies before all queries and media access. Local endpoints retain the existing loopback/trusted-operator security model.

## Evidence and notifications

Only existing, authorized media is linked. Up to three images and a video are shown; unavailable/partial evidence is labeled truthfully. Scoped recognition evidence may be used for an explicitly linked cloud activity only when employee and camera match. WebM and historical MP4 remain supported. Historical records without per-frame capture timestamps return `captured_at: null`; event time is not fabricated as capture time.

The existing durable notification outbox, retry/dedup and SMTP/WhatsApp adapters are reused. Cloud timelines offer notification status. Employee recipient overrides fall back to shop recipients only when absent; an explicit empty channel disables that channel. Existing employee notification-enabled preferences still apply. `CAMERA_EYE_NOTIFICATIONS_TEST_MODE=true` prevents provider calls and returns **not delivered** rather than falsely marking messages sent. Provider exceptions log only their type. QA uses mocked deliveries and sends no real notifications.

## Additive migrations

SQLite initialization adds `attendance_workspace_policies(store_id,person_id,policy_json,updated_at)` with composite primary key, plus `person_events(store_id,person_id,event_time)` index. Existing attendance/evidence/activation records are retained.

PostgreSQL initialization adds `attendance_policies.workspace_rules_json JSONB NOT NULL DEFAULT '{}'`. Existing policy columns and person-policy JSON remain authoritative; added scheduling/overtime settings merge into the new JSON column. Initialization was tested on fresh schemas and by removing only the new column in an isolated schema and rerunning the migration.

## Boundaries and release prerequisites

- **No production release was executed.** Existing user QA records/processes, production databases, secrets and server Compose are untouched.
- Bridge sessions still require explicit checkout. Existing automatic absence workers retain their camera-health checks, grace transitions, feature gates and confirmed CRM 60-minute absence contract. This release does not remove the bridge guard or enable disappearance-based checkout. Completing opt-in automatic checkout for bridge sessions requires a verified cloud-to-edge session-finalization protocol; enabling flags alone does not provide it.
- Local policy edits are explicitly `LOCAL_ONLY`; this release does not overwrite/synchronize cloud policy from the edge. Cloud employee policies use existing inheritance.
- Absence is determined only for a closed date range with a configured calendar; “no recorded login” is not automatically absence. Open working intervals are provisional. Paid-break/payroll deductions are not invented. Overtime is aggregated per employee/day, not multiplied by multiple sessions.
- Reporting reads a bounded history (100,000 records). Exceeding this bound returns 413 rather than silently truncating history; larger installations need a reporting partition/SQL aggregation before rollout.
- CSV is supplied; native Excel export is not added. Read-only review of legacy v1 manual APIs remains available, but their historical contract is unchanged.
- Browser acceptance uses the shared workspace on isolated loopback servers and real APIs, not the complete authenticated portal navigation. It does not verify physical-camera accuracy or live CRM contracts.

Before approved staging rollout: backup the edge SQLite database using SQLite's backup API, and PostgreSQL with the operator's existing `pg_dump`/backup procedure; preserve Compose, `.env`, evidence volumes and activation. Confirm tenant/user Face Login and break permissions using dedicated CRM test identities. Keep `SNAPKEY_CRM_AUTO_LOGOUT_ENABLED=0` and `CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false` until a separately approved end-to-end test.

Rollback: restore the prior application image/checkout while retaining databases and evidence; the extra table/index/JSON column are additive and can remain. Do not drop columns, delete events, mark uncertain deliveries successful, or replay historical events. Restore a database backup only under a separately approved recovery procedure with reconciliation of intervening actions.

## Safe local QA commands

Run from the repository in PowerShell. Use only the existing isolated loopback test database; the fixtures reject other hosts/database names and create/drop their own unique schemas. Do not use the client's QA database or running webcam service.

```powershell
docker compose -p camera-eye-personnel-tests -f compose.postgres-test.local.yml up -d postgres-test
$env:SNAPKEY_TEST_DATABASE_URL='postgresql+psycopg://camera_eye_test:camera-eye-test-only@127.0.0.1:55432/camera_eye_test'
$env:SNAPKEY_REQUIRE_POSTGRES_TESTS='1'
$env:CAMERA_EYE_WORKSPACE_BROWSER='1'
$qaTemp=Join-Path $env:TEMP ('camera-eye-workspace-'+[guid]::NewGuid().ToString('N'))
python -m pytest -q tests/test_attendance_workspace.py tests/test_postgres_attendance_workspace.py tests/test_attendance_station.py tests/test_recognition_attendance.py --basetemp=$qaTemp
```

Browser tests require installed Chrome at its standard Program Files path plus Playwright; they download no browser. Loopback fixture servers stop themselves; no existing Camera Eye service is stopped. Screenshot/CSV artifacts are under `artifacts/attendance-workspace-browser/`. Browser checks cover employee/date/search filtering, timeline, CSV, desktop/mobile overflow, synthetic JPEG preview and playable synthetic WebM. Production notification/CRM delivery is unverified and not claimed.

## Verification and changed files

The relevant regression run completed with **205 passed, 0 failed, 0 skipped** in 99.91 seconds, including isolated PostgreSQL and two real-Chrome checks. Final concurrency/workspace changes were then checked with **24 passed**; complete-session/browser checks with **3 passed**; directly affected frontend/permission checks with **3 passed** (`artifacts/attendance-workspace-ui-final.xml`); final notification/absence regressions with **37 passed**. These runs overlap; do not add their counts as unique tests. `compileall`, JavaScript syntax checks and `git diff --check` passed. Existing framework deprecation warnings and OpenCV's VP8 tag warning did not prevent WebM playback in Chrome.

Regression command (after the environment variables above):

```powershell
$qaTemp=Join-Path $env:TEMP ('camera-eye-regression-'+[guid]::NewGuid().ToString('N'))
$tests=Get-ChildItem tests/test_*.py | Where-Object Name -Match 'attendance|working_time|person_attendance|tokens|notifications|provenance|personnel|evidence|deployment_workflow'
python -m pytest -q $tests.FullName --basetemp=$qaTemp --tb=short
```

Changed implementation files:

- `camera_service/attendance_state.py`, `attendance_workspace.py`, `attendance_workspace_api.py`: shared contract/read model/versioned scoped APIs.
- `camera_service/attendance_engine.py`, `attendance_station.py`, `storage.py`: restart state, atomic event preservation, policy snapshots and break termination.
- `camera_service/api.py`, `web/setup.html`, `web/attendance-assets/workspace.js`, `workspace.css`: local integration/shared responsive UI.
- `cloud_portal/api.py`, `postgres_storage.py`, `notifications.py`: cloud action integration, additive policy storage, safe notification test mode/logs.
- `cloud_portal/static/attendance.html`, `static/js/main.js`: matching controls and failed-delivery feedback; an HTTP 200 with `ok=false` is **not** shown as confirmed attendance.
- `packaging/windows/CameraAutomation.spec`: includes shared UI assets in a future authorized build; no installer was built.
- `tests/test_attendance_workspace.py`, `test_postgres_attendance_workspace.py`, `attendance_browser_support.py`, `test_attendance_workspace_js.py`, `test_notifications.py`, `test_recognition_attendance.py`: new regressions and persisted-state fixtures.
- This document: API, migration, safety boundaries, QA and rollback instructions.

No generated images, biometric fixtures, logs, databases, tokens or user-owned untracked tools are included in the commit.
