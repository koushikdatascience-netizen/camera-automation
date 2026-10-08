# CRM person-wise policy API — implementation handoff

**Status:** Committed on feature branch; not deployed or integration-tested. The API stores person-wise policy values, but the existing presence/checkout worker has **not** yet been migrated to consume them. Do not enable the new 5/15/60-minute policy in production until that migration and the pending CRM absent/logout adapter are complete.

Base URL (after deployment): `https://camera.snapkey.ai`

Authentication: server-to-server header `X-CRM-Integration-Key: <secret>`. Never expose the integration key in browser JavaScript. Route calls through CRM backend. Tenant/shop/user path values must be derived from authenticated CRM context, not untrusted browser input.

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

- Presence worker migration to person policies, episode persistence, two-minute summaries and deduplicated 5/15/60 thresholds.
- Alert read endpoints, attendance summary, evidence manifests and authorized media URLs.
- Authorization hardening: integration key alone does not currently prove tenant/shop entitlement; scope key to permitted tenants before broad rollout.
- Evidence capture completeness and notification delivery verification.
- Day-end calculation and exact gross/net paid break rules.
- CRM absent/auto-logout endpoint supplied by senior; stage adapter and idempotent retries.
- PostgreSQL migration, automated tests and staging rollout.
