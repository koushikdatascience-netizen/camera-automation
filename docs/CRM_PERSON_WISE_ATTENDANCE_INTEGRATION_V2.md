# Camera Eye — Person-wise Attendance and CRM Integration Contract (Draft v2)

Status: **design / implementation handoff; NOT deployed or verified endpoints**. CRM absent/auto-logout upstream endpoint and payload are pending from senior. This document must be updated with verified routes and responses after implementation. Existing CRM operations must remain backward-compatible.

## Business rules

Policy scope: (tenantCode, shopCode, crmUserId), never just shop. Authorized admins may edit. Every policy edit is versioned and auditable.

- AUTO mode: eligible recognition on designated attendance camera can initiate CRM face login, with duplicate check-in suppression.
- MANUAL mode: recognition updates presence only; CHECK_IN / CHECK_OUT require explicit authenticated actions. BREAK_START / BREAK_END remain explicit unless independently specified.
- Presence observations aggregate continuously; persist heartbeat/summary every **2 minutes**, recording lastSeenAt and camera health. Missing heartbeat or offline camera is **UNKNOWN**, not evidence of employee absence.
- After **5 minutes** without detection in the agreed coverage area: open one absence episode and create GRACE_EXCEEDED alert. Do not call logout.
- Count at most one episode per continuous absence; after **more than five** qualifying episodes in the employee's business day, create DAILY_ABSENCE_LIMIT_EXCEEDED alert (dedup once per threshold crossing).
- After **15 minutes**: notify authorized admins by configured email/WhatsApp, with retry/delivery status and idempotency. No repeated messages on every heartbeat.
- After **60 minutes**: mark a prolonged-absence state, create PROLONGED_ABSENCE alert, and queue CRM ABSENT/AUTO_LOGOUT mutation pending its official contract. **Do not invoke existing logout API as a substitute**. Until contract is implemented, keep external mutation PENDING and do not claim CRM checkout success.
- Working target **540 minutes**. Report gross span, explicit breaks, qualifying absence, net worked time, shortfall/overtime separately. Paid/unpaid break rules need approval.
- Day-end auto calculation/closure: pending decision on shift end/cutoff, time zone, overnight shifts, and shared CRM absent/logout API semantics. Do not silently close sessions at midnight.
- Times stored UTC with timezone-aware values; group and display by employee policy's IANA timezone (default Asia/Kolkata). CRM date/time payload must be converted to business-local time.
- Reappearance after 60-minute absence: record RETURNED event, do not invent new login or reopen closed CRM session without a defined re-entry policy.

## Proposed policy API (NOT YET IMPLEMENTED)

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

## Proposed CRM-facing read APIs (NOT YET IMPLEMENTED)

- `GET /portal/v1/attendance/summary?tenantCode=&shopCode=&date=&page=&pageSize=` — daily employee rows, computed minutes, status, evidenceCount, alertCount.
- `GET /portal/v1/attendance/activities?tenantCode=&shopCode=&userId=&from=&to=&type=&page=&pageSize=` — immutable CHECK_IN, CHECK_OUT, BREAK_START, BREAK_END, ABSENCE_STARTED, RETURNED, ABSENT_MARKED, AUTO_LOGOUT_PENDING/COMPLETED events.
- `GET /portal/v1/attendance/presence?tenantCode=&shopCode=&userId=` — lastSeenAt, camera, health, state, episode start, next threshold.
- `GET /portal/v1/attendance/alerts?tenantCode=&shopCode=&userId=&severity=&status=&from=&to=&page=&pageSize=` — searchable alerts, read/acknowledged/resolved, notification delivery.
- `GET /portal/v1/attendance/alerts/{alertId}` — incident timeline and evidence refs.
- `GET /portal/v1/attendance/activities/{activityId}/evidence` — evidence manifest with three images and short clip where captured, explicit missing reason otherwise. Return short-lived authorized media URLs, never raw filesystem paths.
- `GET /portal/v1/attendance/reports/daily?tenantCode=&shopCode=&date=` — gross/net/break/absence/required/shortfall/overtime; policy version used.

These are proposed contracts, not claims of existing API routes. Publish OpenAPI schemas with request/response examples and exact auth rules once code exists.

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
- `employee_presence`: lastSeenAt, lastCameraId, cameraCoverageHealth, currentState, activeEpisodeId.
- `absence_episodes`: unique episodeId, start/end, duration, threshold flags, businessDate, policyVersion, camera coverage and evidence refs.
- `attendance_activities`: immutable actionId, CRM mutation idempotency key/status, actor/source, UTC event time, business date, reason, evidence manifest.
- `attendance_alerts`: alertId, incidentId, category, severity, lifecycle status, occurredAt, resolvedAt, notification status.
- `attendance_evidence`: actionId/alertId, snapshot keys, clip key, capture timestamps, checksum, retention, unavailable reason.
- `outbound_crm_actions`: idempotency key, action kind, payload version, PENDING/SUCCEEDED/FAILED, retries, response redaction.
- `notification_deliveries`: alertId, channel, recipient, attempts, provider status, sentAt.

Ensure indexes on (tenant,shop,user,businessDate), (tenant,shop,occurredAt), incidentId, and idempotency keys. Enforce tenant isolation and RBAC on media access.

## Proof policy

Capture three real frames and a short bounded video per actionable attendance event when the edge can provide them. For absence, use last-seen evidence plus camera-health/observation timeline; do not falsely imply continuous footage of absence. Store explicit `NOT_AVAILABLE` reasons. Limit retention and access to authorized roles. Never block core attendance transaction indefinitely on media upload; show evidence processing state.

## Integration sequence for CRM team

1. Admin opens employee and reads person-wise policy; PUT changes through authenticated API.
2. Attendance page loads daily summary, then paginated activity timeline.
3. Live-presence panel polls/subscribes to presence; displays `UNKNOWN` for offline cameras.
4. Alerts page lists incidents and acknowledgements, opens evidence manifest, loads authorized expiring URLs.
5. Daily report reads server-calculated hours; does not independently infer attendance from alerts.
6. Distinguish CRM sync states PENDING/FAILED/CONFIRMED and do not show an unconfirmed auto-logout as completed.
7. When senior supplies CRM ABSENT/AUTO_LOGOUT method, headers, payload, and response, add adapter and idempotent retries; verify manual and automatic flows against staging.

## Required acceptance tests

Different policies for two employees in same shop; 2-min heartbeat; thresholds at 5/15/60; five vs six absence episodes; camera offline/unknown; return during grace; return after prolonged absence; manual vs auto mode; duplicate recognition; midnight IST vs UTC; overnight shifts; CRM failure/retry; email/WhatsApp delivery; evidence absent/partial/complete; authorization across tenants; re-entry after checkout; 9-hour gross/net calculations.

## Release gate

Do not mark complete until backend routes and migrations exist, OpenAPI is published, tests pass, staging CRM mutations are confirmed, and the CRM team signs off on UI mappings. Do not deploy or rebuild Windows installers solely for this draft.
