# Attendance delivery recovery for deployed commit 2414e38

This runbook is for the production incident reported on 2026-10-09. It does not
authorize production changes. Take a backup, verify CRM externally, obtain the
normal production approval, then use the scoped administrator API. Never update
`edge_events` or `edge_attendance_delivery` directly.

## Confirmed root causes

1. Older edge queue metadata could select a `PERSON_RECOGNIZED` evidence event as
   the predecessor of `ATTENDANCE_ENTRY`. Commit `a413477` stopped creating new bad
   links, but commit `2414e38` still interprets an already persisted link as a hard
   attendance dependency. Recognition events never receive attendance delivery
   receipts, so the entry remains `RETRY`, attempts `0`.
2. Cloud delivery registration occurs after CRM person mapping. When mapping is
   absent or unresolved, the event is inserted into immutable `edge_events`, the
   API returns `MAPPING_REQUIRED`, and no `edge_attendance_delivery` row is made.
   A later `BREAK_END` can therefore reference an ingested `BREAK_START` with no
   delivery receipt. The six dependent records then wait behind those roots.
3. The `attempts=0` roots have not acquired the CRM dispatch claim in this bridge.
   That is strong evidence that this bridge did not dispatch them, but a missing
   historical receipt is not proof that CRM has no matching action. Missing real
   attendance predecessors are therefore quarantined for operator reconciliation.

The fix registers a receipt before mapping or dispatch, records mapping blockers,
keeps original predecessor IDs for audit, and adds an effective predecessor used
for ordering. A same-person `PERSON_RECOGNIZED` predecessor is ignored only for an
`ATTENDANCE_ENTRY`. A missing attendance predecessor found in `edge_events` is
registered as `RECONCILIATION_REQUIRED` and is never replayed automatically.

## Read-only verification

Run in a read-only PostgreSQL transaction. The JSON expressions select identity
and ordering fields only; they do not return images, embeddings, tokens, or full
personnel records.

```sql
BEGIN TRANSACTION READ ONLY;

SELECT
  e.id,
  e.tenant_id,
  e.shop_id,
  e.edge_id,
  e.camera_id,
  e.event_type,
  e.event_time,
  e.payload_json->'payload'->>'person_id' AS local_person_id,
  e.payload_json->'payload'->'metadata'->>'attendance_session_id' AS session_id,
  e.payload_json->'payload'->'metadata'->>'attendance_source' AS attendance_source,
  e.payload_json->'payload'->'metadata'->>'predecessor_event_id' AS predecessor_id,
  d.status,
  d.attempts,
  d.effective_predecessor_id,
  d.error_code,
  d.registration_reason
FROM edge_events e
LEFT JOIN edge_attendance_delivery d ON d.event_id=e.id
WHERE e.id IN (
  '16c0f5c2-561d-4b9c-bd6e-4be62edad0eb',
  'df8dc6c0-f320-4fa6-a390-65416cb8bc98',
  'manual:81937ad2-f621-41c8-bb1f-925da0ef0911',
  'manual:ea98a457-7711-4c02-a8db-a110d52df2db'
)
ORDER BY e.event_time;

WITH RECURSIVE blocked AS (
  SELECT d.event_id,d.predecessor_id,d.status,d.attempts,d.error_code,1 AS depth
  FROM edge_attendance_delivery d
  WHERE d.predecessor_id IN (
    '16c0f5c2-561d-4b9c-bd6e-4be62edad0eb',
    'manual:81937ad2-f621-41c8-bb1f-925da0ef0911',
    'manual:ea98a457-7711-4c02-a8db-a110d52df2db'
  )
  UNION ALL
  SELECT d.event_id,d.predecessor_id,d.status,d.attempts,d.error_code,b.depth+1
  FROM edge_attendance_delivery d
  JOIN blocked b ON d.predecessor_id=b.event_id
  WHERE b.depth < 20
)
SELECT * FROM blocked ORDER BY depth,event_id;

SELECT tenant_id,shop_id,local_person_id,crm_user_id,break_master_id,enabled
FROM crm_person_mappings
WHERE enabled=TRUE
  AND (tenant_id,shop_id,local_person_id) IN (
    SELECT e.tenant_id,e.shop_id,e.payload_json->'payload'->>'person_id'
    FROM edge_events e
    WHERE e.id IN (
      '16c0f5c2-561d-4b9c-bd6e-4be62edad0eb',
      'manual:81937ad2-f621-41c8-bb1f-925da0ef0911',
      'manual:ea98a457-7711-4c02-a8db-a110d52df2db'
    )
  );

COMMIT;
```

After deploying the fix, repeat the first query. Expected results:

- automatic entry `16c0...` retains `predecessor_id=df8d...`, has
  `effective_predecessor_id=NULL`, and registration reason
  `IGNORED_NON_ATTENDANCE_PREDECESSOR`;
- missing `BREAK_START` `manual:ea98...` has a receipt with status
  `RECONCILIATION_REQUIRED`, reason `HISTORICAL_RECEIPT_MISSING`, attempts `0`;
- `BREAK_END` and the six descendants remain blocked until that root is reconciled.

## Backup before approved deployment

Run as the authorized server administrator. This reads the existing container
credentials internally and does not print them.

```bash
cd /opt/camera-eye
umask 077
mkdir -p /var/backups/camera-eye
backup="/var/backups/camera-eye/attendance-delivery-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose --project-name camera-eye --env-file .env -f docker-compose.cloud.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup"
test -s "$backup"
docker inspect --format '{{.Image}}' \
  "$(docker compose --project-name camera-eye --env-file .env -f docker-compose.cloud.yml ps -q portal)"
```

## Operator reconciliation

The scoped readiness endpoint reports booleans and blocker codes without returning
credentials:

```text
GET /portal/v1/tenants/{tenant_id}/attendance-sync/{event_id}/readiness
```

Check each root. Dispatch can still be blocked by missing CRM mapping, missing
token-encryption key, unavailable Face Login configuration, an AUTO employee in
MANUAL policy, disabled automatic login, or a missing break-master mapping. The
production SSH account used during investigation cannot access Docker or `.env`,
so actual runtime flag values were not read. The endpoint provides the authorized,
secret-free verification after deployment. Relevant settings are:

- `SNAPKEY_CRM_AUTO_LOGIN_ENABLED=1` for recognition check-in;
- `CAMERA_EYE_TOKEN_ENCRYPTION_KEY` configured;
- employee tenant/shop mapping present and identity-consistent;
- employee attendance policy `AUTO` for recognition check-in;
- `break_master_id` present for `BREAK_START`.

Manual entry, exit and breaks do not depend on the automatic-login/logout flags.
Bridge disappearance never creates checkout. Definite CRM rejection retries with
a safe reason code; timeout/server uncertainty remains reconciliation-required.

For `16c0...`, verify readiness and allow the edge retry. The receipt already has
attempts `0`, and the ignored predecessor is only recognition evidence. Confirm
that exactly one CRM check-in and one cloud `CHECK_IN` activity result.

For `manual:ea98...`, first compare the employee, tenant, shop, action, event time
and session with CRM. Then make exactly one audited decision:

```text
POST /portal/v1/tenants/{tenant_id}/attendance-sync/manual:ea98a457-7711-4c02-a8db-a110d52df2db/reconcile
Content-Type: application/json

{"crm_applied":true,"justification":"Verified matching BREAK_START in CRM at <time>"}
```

Use `crm_applied=true` only when the matching CRM action exists. It finalizes local
state without calling CRM. If the matching action definitely does not exist, use:

```json
{"crm_applied":false,"justification":"Verified CRM has no matching BREAK_START for employee/session/time"}
```

That explicit operator decision authorizes one idempotently claimed CRM attempt.
Do not reconcile `BREAK_END` first. Once `BREAK_START` is `SUCCEEDED`, edge retries
advance `BREAK_END` and the remaining events in predecessor order. Re-run the
read-only chain query and verify that attempts increment only for actual dispatch,
every receipt reaches `SUCCEEDED`, and no duplicate CRM rows appear.

## Rollback

Record the previous image before deployment. If application health fails, restore
that image using the existing no-volume replacement process:

```bash
cd /opt/camera-eye
previous_image=$(cat /var/lib/camera-eye-ci/previous-image)
test -n "$previous_image"
docker image inspect "$previous_image" >/dev/null
override=$(mktemp /tmp/camera-eye-rollback.XXXXXXXX.yml)
printf 'services:\n  portal:\n    image: "%s"\n' "$previous_image" > "$override"
docker compose --project-name camera-eye --env-file .env -f docker-compose.cloud.yml \
  -f "$override" up -d --no-deps --no-build --pull never portal
curl -fsS --max-time 10 http://127.0.0.1:8000/health
```

The migration is additive; preserve `edge_attendance_delivery`, `edge_events`, all
attendance tables and volumes. Do not restore an old database dump over live data.
After rollback, pause attendance retries for affected chains: commit `2414e38`
cannot interpret the new effective predecessor field and will block again. Resume
only on the fixed cloud version. The fix is cloud-side and accepts old edge events,
so no Windows EXE rebuild is required.
