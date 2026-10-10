# Attendance bridge release gate

This release extends the existing attendance engine; it does not deploy production.

## Implemented

- Policy/day-end CRM checkout queues an idempotent, scoped EXE command and a confirmed
  delivery receipt in the same PostgreSQL transaction as cloud checkout. Recovery
  after confirmed CRM success never repeats the CRM request.
- Cloud manual actions mirror their original event/session identity into SQLite.
  EXE acknowledgements reuse the cloud receipt, rather than submitting CRM twice.
- Newer recognition or a different local session produces reconciliation, not closure
  of another session. Old EXEs cannot receive automatic bridge checkout until they
  advertise the new capability. Offline commands retain the existing polling lease.
- Manual logout suppresses AUTO reopening for the rest of the shop's calendar day.
  Explicit manual check-in clears that hold. Existing history remains intact.
- Qualifying 60-minute absence records a sticky full-day ABSENT decision, independent
  of CRM availability. Authorized administrators can correct classification with an
  audit note; correction does not mutate CRM. Heartbeat mirrors decisions to EXEs.
- Camera outage, inference errors and monitoring interruption reset the qualifying
  coverage clock. Observation heartbeats do not reopen checked-out attendance.
- Unconfigured working-time default is nine hours; explicit existing policies remain.
- Employee policy overrides are respected for day-end checkout, including overnight
  deadlines. Local/cloud policy UI exposes the camera thresholds and day-end setting.
- Stable policy-alert identifiers and existing outbox uniqueness prevent repeated
  episode/day messages. CRM failures requiring intervention are surfaced as alerts.

## Additive storage changes

PostgreSQL: `attendance_camera_coverage.healthy_since`; partial unique index
`idx_full_day_absence_once`. Existing transition, command, receipt and notification
tables retain their authority and data. SQLite: `attendance_auto_holds`,
`attendance_observations`, `attendance_day_decisions`. No history reset.

## Actual verification

Results are recorded in the release handoff. Tests use synthetic identities, mocked
CRM and a disposable PostgreSQL schema on an explicitly named local test database.
They are not live CRM or real-camera verification of these new commands.

Executed locally (overlapping batches; do not sum as distinct tests):

- Initial rules/defects/policy/bridge batch: **57 passed**.
- Critical attendance, PostgreSQL bridge/integration/workspace, CRM logout,
  notifications, media and onboarding batch: **94 passed, 1 skipped**.
- Follow-up bridge/defects/policy batch: **48 passed**.
- Final `test_person_attendance_rules.py test_policy_checkout_release.py`:
  **30 passed**, including configurable shop-day boundary and isolated PostgreSQL.
- Targeted cloud manual checkout/EXE acknowledgement: **1 passed**.
- `test_attendance_workspace.py test_attendance_workspace_js.py
  test_cloud_camera_sync.py test_deployment_workflow_safety.py`: **34 passed, 1 skipped**.
- `python -m compileall -q cloud_portal camera_service`, `node --check
  camera_service/web/attendance-assets/workspace.js`, and `git diff --check`: passed.

Pytest used workspace-local `--basetemp` directories. PostgreSQL DSN targeted only
`127.0.0.1:55439/camera_eye_test`; integration fixtures created/dropped disposable
schemas. Skips are not counted as verified. No complete-suite rerun was performed.

## Remaining acceptance gates

1. CRM `/api/UserActivity/auto-logout`: complete successful response and confirmation
   that Bearer authorization accepts the same employee Face Login token. The earlier
   `{"success"}` excerpt is not valid JSON and cannot prove this contract.
2. Build/install the updated EXE and perform a controlled command/receipt smoke test.
   Old installed EXEs do not contain the new command handler.
3. Final immutable cloud image and controlled production approval. SMTP/WhatsApp
   live delivery needs configured providers; mocked results cannot prove it.

Evidence remains truthful: delayed absence actions reference available last-seen
media and explicitly identify missing fresh capture. They do not manufacture three
frames or a clip when the employee/camera is unavailable. Business-day classification follows the shop timezone and configurable attendance-day
start, defaulting to midnight.

## Deployment and rollback

Use `docs/CAMERA_EYE_PRODUCTION_HANDOFF.md` for the reviewed image-only procedure.
Preserve BOTH `/opt/camera-eye/docker-compose.cloud.yml` and
`/tmp/camera-eye-attendance-recovery.yml`, `.env`, keys, volumes and nine quarantined
historical receipts. Do not use the installed Compose-overwriting deployment handler.

Before approved deployment, verify the existing backup package and restore result at
`/opt/camera-eye/backups/final-release-a531-gsjv454n`, then create a fresh database
backup. Build `Dockerfile.cloud` from this exact commit with revision label and record
the immutable image ID; the earlier `2404ed8` image lacks these changes.

Keep `SNAPKEY_CRM_AUTO_LOGIN_ENABLED=0`, `SNAPKEY_CRM_AUTO_LOGOUT_ENABLED=0`, and
`CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false` in the release override until activation
is separately approved for verified contracts. Approval is required before migration,
portal replacement or production automation. Image-only rollback retains additive
schema/history, disabled flags and quarantine; never delete volumes or replay history.

Windows: manually run `Camera Eye Windows Release` on the release branch with
`publish=false`; test the installer before approving update-registry publication.
GitHub push alone does not trigger cloud deployment or Windows publishing.
