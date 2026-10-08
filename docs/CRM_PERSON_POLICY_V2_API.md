# CRM person-wise policy API — implementation handoff

**Status:** Implemented on the feature branch; not deployed or verified against live CRM/PostgreSQL. The worker evaluates person-wise 5/15/60-minute absence transitions. The separate CRM auto-logout remains disabled by default and requires staging contract validation. Daily report and evidence/notification limitations are documented in `CAMERA_EYE_V2_STAGING_AND_ROLLBACK.md`.

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

## Outstanding before CRM frontend rollout

- End-to-end worker checks under PostgreSQL for delayed heartbeats, camera-specific coverage, concurrency, and full-day policy boundaries.
- Signed/authenticated media delivery and evidence capture completeness; V2 currently returns only a scoped manifest.
- Per-person notification preferences, durable outbox/retry/dedup/rate limits, and delivery status; current sends are shop-wide and synchronous.
- Approved payroll policy for absence deduction and paid/unpaid break treatment.
- Staging verification of the CRM response contract and reconciliation path. Ambiguous auto-logout results are not blindly retried.
- Backup/restore and previous-schema upgrade verification on PostgreSQL; SQLite tests do not establish migration readiness.
- PostgreSQL migration, automated tests and staging rollout.
