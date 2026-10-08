# CRM person-wise policy API — implementation handoff

**Status:** Implemented on the feature branch; not deployed or verified against staging/live CRM. Person-specific thresholds drive absence transitions; the separate CRM auto-logout has a fixed 60-minute contract and remains disabled by default pending staging validation.

Base URL (after deployment): `https://camera.snapkey.ai`

Authentication: server-to-server header `X-CRM-Integration-Key: <secret>`. Never expose the integration key in browser JavaScript. In production, also configure `SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES`; each tenant/shop path must match this server allow-list. Route calls through CRM backend. Tenant/shop/user path values must be derived from authenticated CRM context, not untrusted browser input.

## Save or update policy

`PUT /integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/policy`

```json
{
  "attendanceMode": "AUTO",
  "presenceUpdateIntervalMinutes": 2,
  "outOfCameraGraceMinutes": 5,
  "maxOutOfCameraOccurrencesPerDay": 5,
  "adminNotificationAfterMinutes": 15,
  "markAbsentAfterMinutes": 60,
  "requiredWorkingMinutes": 540,
  "dayEndAutoLogoutEnabled": true,
  "absenceMonitoringEnabled": true,
  "timezone": "Asia/Kolkata",
  "emailNotificationsEnabled": true,
  "whatsappNotificationsEnabled": true
}
```

`attendanceMode` accepts `AUTO` or `MANUAL`. Threshold order must satisfy `grace < notification < absent`.

Success: HTTP 200, `{"policy":{...}}`, including `tenantCode`, `shopCode`, `userId`, `version`, and `updatedAt`.

Failures: HTTP 401 for invalid integration key; HTTP 422 for validation failure; HTTP 503 if the active store does not support person policies.

## Read policy

`GET /integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/policy`

Success: HTTP 200 `{"policy":{...}}`; HTTP 404 if no person-specific policy exists. This endpoint intentionally does not silently fall back to the legacy shop policy.

## Existing daily activity endpoint

`GET /integration/v1/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/daily-activity?day=2026-10-08`

This pre-existing route lists attendance activity for a single CRM user on a business date. Its event completeness and evidence links still require integration verification.

## Action evidence and CRM reconciliation

`GET /integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/activities/{activity_id}/evidence` returns an explicit evidence status, up to three snapshot references, a short-video reference, missing reasons, and authenticated proxy paths when media is available. Fetch media through the snapshot and video subroutes using the same server-side `X-CRM-Integration-Key`; do not expose the key in browser code.

`GET .../auto-logout-actions` lists unresolved action status without exposing credentials. If the CRM mutation times out or local finalization is interrupted, check the employee's state in CRM before resolving with `POST .../auto-logout-actions/{action_id}/reconcile` and `{"outcome":"CRM_CONFIRMED"}` or `{"outcome":"CRM_NOT_APPLIED"}`. `CRM_CONFIRMED` completes local attendance and action state transactionally; `CRM_NOT_APPLIED` requeues only a safe retry. No ambiguous request is automatically replayed.

## Outstanding before CRM frontend rollout

- End-to-end worker checks under PostgreSQL for delayed heartbeats, camera-specific coverage, concurrency, and full-day policy boundaries.
- Staging verification of three-snapshot/short-clip capture, missing-evidence reporting, and authenticated media proxy access. A delayed absence checkout can link last-seen evidence but cannot capture a frame from the past.
- Notifications use a durable PostgreSQL outbox with retry and delivery counts. Recipient lists still come from shop policy; per-person recipient routing and rate limiting are outstanding.
- Approved payroll policy for absence deduction and paid/unpaid break treatment.
- Staging verification of the CRM response contract and reconciliation path. Ambiguous auto-logout results are not blindly retried.
- Backup/restore and previous-schema upgrade verification on PostgreSQL; SQLite tests do not establish migration readiness.
- The startup DDL adds the outbox, zone-coverage status, and CRM action-recovery fields idempotently. Run `tests/test_postgres_attendance_integration.py` against a disposable test PostgreSQL database and verify backup/restore and an upgrade from the previous schema in staging.
