# Attendance synchronization bridge

Branch: `feat/attendance-sync-bridge-20261009`. Cloud deployment and Windows
release are separate approval steps. No installer is built by this change.

## What changes

The existing local station commits its attendance session, manual audit event
and edge queue event in one SQLite transaction. Canonical events are
`ATTENDANCE_ENTRY`, `ATTENDANCE_EXIT`, `BREAK_START`, and `BREAK_END`.
The existing explicit break routes use the same transaction and session.
AUTO recognition confirms entry once; MANUAL recognition only offers a candidate.
Per-person cloud mode overrides the edge default. Missing mode in an older cloud
personnel response fails safe to MANUAL. Track disappearance never closes a bridge
session, even if absence logout flags are enabled. Existing non-bridge policy
workflows and maximum-logoff rules retain their prior behavior.

Each event has an immutable event ID, UUID attendance session, explicit source
and predecessor event. Cloud delivery follows that chain, preventing a checkout
or break from overtaking check-in. Same-day re-entry uses another session UUID.
Changing an event's person, tenant, camera, session or source on retry is rejected.
Another event ID cannot repeat the same session entry/exit or branch the chain.

The cloud resolves the local personnel ID using its existing **tenant + shop**
CRM mapping. Client-supplied CRM user IDs are ignored. No cross-tenant lookup or
email/code guessing is permitted. Existing encrypted, scoped employee Face Login
token handling is reused. Both automatic-logout flags remain off in deployment.

## API and CRM contracts

No CRM endpoint or payload is changed:

| Operation | Existing endpoint | Authentication |
| --- | --- | --- |
| Personnel identity | `GET /api/User/face-embeddings/{tenantCode}` | Confirmed unauthenticated directory contract |
| Employee authentication | `POST /api/Auth/loginUsingFaceTenant` | Existing enrolled image + tenant UUID |
| Entry/exit | `POST /api/UserRoster/LoginLogout` | Same employee's Face Login bearer token |
| Break start | `POST /api/UserBreak/start-break` | Same employee's token; server-mapped break master |
| Break end | `POST /api/UserBreak/end-break` | Same employee's token |

Monthly roster remains separate. Disappearance does not call CRM auto-logout for
bridge sessions. Static service credentials are not used for bridge actions.

Existing `POST /edge/v1/events` receives the normal `edge.event.v1` envelope with
local payload metadata:

```json
{
  "attendance_sync_bridge": true,
  "attendance_session_id": "stable-session-uuid",
  "attendance_source": "MANUAL",
  "attendance_mode": "MANUAL",
  "predecessor_event_id": "previous-event-or-null"
}
```

Its response adds `attendance_sync.status`. The edge marks delivery SYNCED only
for `SUCCEEDED`. A missing receipt (including an older cloud server) remains
pending. Duplicate posts retry delivery without inserting another attendance
event. Authentication or definite CRM rejection retries with backoff. Read/write
timeouts, server errors and interrupted in-flight mutations require reconciliation.
`CRM_CONFIRMED` retries local presence/activity finalization without calling CRM.
The persisted edge queue owns retries; a running edge sync worker is required.

Scoped administrator APIs:

- `GET /portal/v1/tenants/{tenant_id}/attendance-sync/{event_id}`: delivery status.
- `POST /portal/v1/tenants/{tenant_id}/attendance-sync/{event_id}/reconcile`:
  `{"crm_applied": true, "justification": "Verified matching action in CRM"}`.
  Requires OWNER/ADMIN/SUPERADMIN for the event's tenant and shop. It records the
  administrator and explanation. Confirming applied schedules only local recovery
  on the next edge retry. Confirming not applied permits a new CRM attempt.
  **Check employee, tenant, timestamp and action in CRM before using this API.**
  There is no invented CRM reconciliation endpoint or automatic uncertain replay.

Evidence references are retained. AUTO entry inherits completion of its linked
recognition snapshots and clip. Manual actions retain their captured snapshot and
available recognition evidence. The existing uploader transfers media and returns
scoped references; filesystem paths and candidate tokens are stripped in transit.
Missing snapshots/clips are explicit; no fabricated evidence is produced. Older
incidents, attendance sessions and activation records are not rewritten or deleted.

## Additive migrations

- Edge SQLite: optional `personnel.attendance_mode` column.
- Cloud PostgreSQL/SQLite: `edge_attendance_delivery`, a receipt/claim table for
  existing edge events, with scope, session, predecessor, status, attempts,
  claim/retry times, safe error codes and administrator reconciliation audit.
- Indexes enforce unique session entry/exit and a single predecessor successor;
  a person/scope index supports claim checks. PostgreSQL transaction advisory locks
  serialize receipt claims across workers.
- Existing `attendance_activity`, `attendance_presence`, `attendance_sessions`
  and `edge_events` remain the attendance sources. Initialization upgrades tables
  additively; no database or history reset is needed.
- PostgreSQL tests create disposable per-connection schemas; they do not alter
  database-wide `search_path` or use production data.

Keep `CAMERA_EYE_TOKEN_ENCRYPTION_KEY` configured using the existing secure secret
store. Keep `SNAPKEY_CRM_AUTO_LOGOUT_ENABLED=0` and
`CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false`. AUTO entry additionally requires
`SNAPKEY_CRM_AUTO_LOGIN_ENABLED=1`; explicit MANUAL actions do not depend on that flag.
Unmapped historical local IDs require a scoped mapping using the existing mapping
management workflow; they are not silently guessed or converted.

## Deployment commands for review — do not run without approval

Validation on 2026-10-09:

- `python -m compileall -q camera_service cloud_portal`: passed.
- Complete suite with `SNAPKEY_REQUIRE_POSTGRES_TESTS=1` and the isolated PostgreSQL
  test DSN on the VPS: **290 passed, zero failed, zero skipped**. All CRM mutations
  are mocked; production services/databases were not used.
- Windows full suite: **283 passed, four PostgreSQL tests skipped** because the
  local PostgreSQL test DSN was absent. The final additional evidence cases were
  included in the complete VPS run. There is one dependency deprecation warning.
- Real webcam and live CRM staging verification remain required before releasing
  updated edge software. Ambiguous remote outcomes require administrator review;
  an offline edge cannot drain its queue. No Windows installer was built.

After verifying the reported commit and tests, manually dispatch the existing
workflow at the exact pushed development branch. Its production environment must
have required reviewers configured. A push does not trigger deployment.

```powershell
gh workflow run cloud-deploy.yml --repo koushikdatascience-netizen/camera-automation --ref feat/attendance-sync-bridge-20261009
gh run list --repo koushikdatascience-netizen/camera-automation --workflow cloud-deploy.yml --limit 3
```

The workflow validates code, builds and smoke-tests an immutable SHA-tagged cloud
image with isolated PostgreSQL, then uses the existing restricted deployment helper.
It does not rebuild the Windows installer. Keep edge bridge testing in staging;
release updated edge software only after cloud verification and separate approval.

Before approved production deployment, a server administrator should take a
restricted backup and record the portal image. These commands use existing server
credentials inside the PostgreSQL container without displaying them:

```bash
cd /opt/camera-eye
umask 077
mkdir -p /var/backups/camera-eye
docker compose --project-name camera-eye --env-file .env -f docker-compose.cloud.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "/var/backups/camera-eye/attendance-bridge-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker inspect --format '{{.Image}}' \
  "$(docker compose --project-name camera-eye --env-file .env -f docker-compose.cloud.yml ps -q portal)"
```

Rollback after a successful deployment, executed only by an authorized server
administrator with Docker access:

```bash
cd /opt/camera-eye
umask 077
previous_image=$(cat /var/lib/camera-eye-ci/previous-image)
test -n "$previous_image"
docker image inspect "$previous_image" >/dev/null
override=$(mktemp /tmp/camera-eye-rollback.XXXXXXXX.yml)
printf 'services:\n  portal:\n    image: "%s"\n' "$previous_image" > "$override"
docker compose --project-name camera-eye --env-file .env -f docker-compose.cloud.yml \
  -f "$override" up -d --no-deps --no-build --pull never portal
curl -fsS --max-time 10 http://127.0.0.1:8000/health
```

Preserve additive tables and volumes; do not restore an older database over new
attendance. If updated edges have already been released, pause their sync workers
before rolling back to a cloud version without bridge receipts. Otherwise that
older server can mutate CRM without recording a bridge receipt, making a later
retry ambiguous. Preserve pending queues and reconcile uncertain actions before
resuming against a bridge-capable cloud version.
