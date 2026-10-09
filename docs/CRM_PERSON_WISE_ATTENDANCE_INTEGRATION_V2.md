# Camera Eye — Person-wise Attendance and CRM Integration Contract (V2)

Status: **implemented in this feature branch; not deployed or verified against staging/live CRM**. Existing V1 CRM operations remain available. Production release still requires staging PostgreSQL, CRM contract verification, and edge packaging/client validation. `CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED` must remain `false` until those checks and explicit approval.

## Business rules

Policy scope: (tenantCode, shopCode, crmUserId), never just shop. Authorized admins may edit. Every policy edit is versioned and auditable.

- AUTO mode: eligible recognition on designated attendance camera can initiate CRM face login, with duplicate check-in suppression.
- MANUAL mode: recognition alone never mutates CRM attendance. A successful authenticated manual CHECK_IN opens the monitored presence session; absence evaluation and alerts continue unless `absenceMonitoringEnabled` is false. CHECK_OUT and break actions remain explicit.
- Presence observations aggregate continuously; persist heartbeat/summary every **2 minutes**, recording lastSeenAt and camera health. Missing heartbeat or offline camera is **UNKNOWN**, not evidence of employee absence.
- After **5 minutes** without detection in the agreed coverage area: open one absence episode and create GRACE_EXCEEDED alert. Do not call logout.
- Count at most one episode per continuous absence; after **more than five** qualifying episodes in the employee's business day, create DAILY_ABSENCE_LIMIT_EXCEEDED alert (dedup once per threshold crossing).
- After **15 minutes**: notify authorized admins by configured email/WhatsApp, with retry/delivery status and idempotency. No repeated messages on every heartbeat.
- `markAbsentAfterMinutes` is person-specific and controls the prolonged-absence transition. The separate `/api/UserActivity/auto-logout` CRM endpoint has a fixed **60-minute minimum** contract. Camera Eye calls it only after at least 60 minutes, healthy attendance-camera coverage in the person's configured camera zone, AUTO mode, and explicit feature enablement. It is not interchangeable with `/api/UserRoster/LoginLogout`. Ambiguous results are not replayed; the action is exposed for explicit CRM reconciliation.
- Working target **540 minutes**. Report gross span, explicit breaks, qualifying absence, net worked time, shortfall/overtime separately. Paid/unpaid break rules need approval.
- Daily reports clip intervals to the employee's local business date, include preceding-day activity for overnight shifts, and count an open shift provisionally through now. No session is silently closed at midnight.
- Times stored UTC with timezone-aware values; group and display by employee policy's IANA timezone (default Asia/Kolkata). CRM date/time payload must be converted to business-local time.
- Reappearance after 60-minute absence: record RETURNED event, do not invent new login or reopen closed CRM session without a defined re-entry policy.

## Implemented person-wise policy API

`PUT /portal/v1/attendance/policies/{crmUserId}`
`GET /portal/v1/attendance/policies/{crmUserId}?tenantCode=...&shopCode=...`
`GET /portal/v1/attendance/policies?tenantCode=...&shopCode=...&page=1&pageSize=25`

```json
{
  "tenantCode": "ABM-46-775",
  "shopCode": "abm-46-775",
  "userId": "crm-user-uuid",
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

Validate ordered thresholds grace < notification < absent, integer bounds, timezone, shop membership, and authenticated admin scope. Resolve policy per employee; do not fall back to a shared shop policy without an explicit versioned fallback rule.

## Implemented CRM-facing read APIs (feature branch; not production validated)

- `PUT/GET /integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/policy` — versioned policy.
- `GET .../activities?day=&page=&page_size=` — paged per-person activity.
- `GET .../presence` — current stored presence and the latest zone-matched camera coverage state (`HEALTHY` or `UNKNOWN`).
- `GET .../daily-summary?day=` — timezone/day-clipped duration report; absence payroll treatment remains unapplied.
- `GET .../activities/{activity_id}/evidence` — evidence manifest with snapshot/clip status and scoped authenticated proxy URLs when uploaded.
- `GET .../activities/{activity_id}/evidence/snapshots/{index}` and `GET .../evidence/video` — authenticated tenant/shop-scoped media proxy; no filesystem path or public storage key is returned.
- `GET .../alerts?day=&limit=` — absence threshold alerts with durable notification outbox status.
- `GET .../auto-logout-actions` and `POST .../auto-logout-actions/{action_id}/reconcile` — inspect unresolved CRM outcomes; after checking CRM, report `CRM_CONFIRMED` or `CRM_NOT_APPLIED`. These actions do not bypass the global auto-logout feature flag.

All routes require the server-side `X-CRM-Integration-Key`. In production, configure `SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES` as a JSON list of tenant IDs and permitted shop IDs. Never expose the integration key in browser JavaScript. Media proxy URLs require the same scoped server-side key.

## Suggested response envelope

```json
{
  "success": true,
  "data": {
    "activityId": "activity-id",
    "incidentId": "incident-id",
    "userId": "crm-user-uuid",
    "shopCode": "abm-46-775",
    "type": "CHECK_IN",
    "source": "CAMERA_EYE",
    "occurredAt": "2026-10-08T04:30:00Z",
    "businessDate": "2026-10-08",
    "timezone": "Asia/Kolkata",
    "crmSyncStatus": "CONFIRMED",
    "evidence": {"images": [], "video": null, "status": "PENDING_CAPTURE"}
  },
  "requestId": "request-id"
}
```

The sample envelope is illustrative only; exact response shape must be agreed and tested.

## Data model

- `employee_attendance_policies`: tenant/shop/user composite key, policy fields, version, updatedBy, timestamps.
- `attendance_presence`: checked-in/break state, last seen event, camera ID and zone; `attendance_camera_coverage` stores current HEALTHY/UNKNOWN status and reason.
- `absence_episodes`: unique episodeId, start/end, duration, threshold flags, businessDate, policyVersion, camera coverage and evidence refs.
- `attendance_activities`: immutable actionId, CRM mutation idempotency key/status, actor/source, UTC event time, business date, reason, evidence manifest.
- `attendance_alerts`: alertId, incidentId, category, severity, lifecycle status, occurredAt, resolvedAt, notification status.
- `attendance_evidence`: actionId/alertId, snapshot keys, clip key, capture timestamps, checksum, retention, unavailable reason.
- `outbound_crm_actions`: idempotency key, action kind, payload version, PENDING/SUCCEEDED/FAILED, retries, response redaction.
- `notification_outbox`: idempotent per-event/channel/recipient rows with attempts, next retry, status, last error, and delivery time.

Ensure indexes on (tenant,shop,user,businessDate), (tenant,shop,occurredAt), incidentId, and idempotency keys. Enforce tenant isolation and RBAC on media access.

## Proof policy

The edge captures up to three frames and a short bounded video for recognition events linked to attendance actions. Missing or interrupted assets carry explicit reasons. For delayed absence checkout, use last-seen evidence and say no new frame was captured at checkout time; do not imply continuous footage of absence. Media is available only through authenticated scoped proxy endpoints.

## Integration sequence for CRM team

1. Admin opens employee and reads person-wise policy; PUT changes through authenticated API.
2. Attendance page loads daily summary, then paginated activity timeline.
3. Live-presence panel polls/subscribes to presence; displays `UNKNOWN` for offline cameras.
4. Alerts page lists incidents and delivery status; the CRM backend can fetch action evidence through the scoped authenticated proxy.
5. Daily report reads server-calculated hours; does not independently infer attendance from alerts.
6. Distinguish CRM sync states PENDING/FAILED/CONFIRMED and do not show an unconfirmed auto-logout as completed.
7. For an ambiguous auto-logout, CRM operations first check CRM state, then post `CRM_CONFIRMED` or `CRM_NOT_APPLIED` to the action reconciliation route. Never replay without confirming the remote result.

## Required acceptance tests

Different policies for two employees in same shop; 2-min heartbeat; thresholds at 5/15/60; five vs six absence episodes; camera offline/unknown; return during grace; return after prolonged absence; manual vs auto mode; duplicate recognition; midnight IST vs UTC; overnight shifts; CRM failure/retry; email/WhatsApp delivery; evidence absent/partial/complete; authorization across tenants; re-entry after checkout; 9-hour gross/net calculations.

## Release gate

Do not mark complete until backend routes and migrations exist, OpenAPI is published, tests pass, staging CRM mutations are confirmed, and the CRM team signs off on UI mappings. Do not deploy or rebuild Windows installers solely for this draft.
