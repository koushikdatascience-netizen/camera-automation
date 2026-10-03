# CRM Camera Eye Integration

## Ownership And Scope

Tenant/company -> shop -> edge -> camera. Camera IPs are private LAN addresses and not global identifiers. Camera identity includes tenant, shop, edge and camera ID. The cloud authenticates portal sessions and scoped edge tokens independently.

The edge runs capture, detector, ByteTrack, recognition, attendance crossing rules, alarms, SQLite, local evidence and a retrying event outbox. One worker owns each saved camera; HTTP viewers share its output. Cloud stores configuration, heartbeats, real events, image evidence, personnel/mappings and portal sessions. Cloud configuration is polled and unchanged settings do not restart workers.

## Authentication

Trusted CRM backend calls POST /crm/session with X-CRM-Integration-Key and tenantId/companyCode, shopCode, userId, displayName and role. Never expose that key to the CRM frontend. With companyCode supplied, canonical tenant is tenant-{company slug}; shopCode is canonicalized to a lowercase slug. Do not assume legacy IDs are unchanged: verify /session/status.

The response supplies launchUrl with a session fragment. The Camera Eye frontend stores the session in sessionStorage, removes the fragment and sends Authorization: Bearer on portal API calls. Sessions are hashed server-side and scope is enforced from the authenticated principal, not query-string tenant/shop values.

Owners/admins can create shop-scoped expiring one-time edge codes via POST /portal/v1/tenants/{tenant}/edge-activation-codes. The edge activates over HTTPS, verifies a machine/tenant/site/edge-bound signed license, persists configuration, and starts cloud sync. Global shared codes are development-only for edge activation.

## Reusable UI

| Page | Reuse | Transport |
| --- | --- | --- |
| Overview | Camera health, edge health, recent activity, counts | Authenticated portal summary/edges/events |
| Live Cameras | Layout/tile/status controls | Local edge MJPEG only; cloud inventory has no remote video |
| Camera Detail | Local focused/fullscreen camera tile or direct AI view | No dedicated cloud remote camera workspace yet |
| Camera Setup | Form, AI flags, configuration cards | Portal camera registry -> edge polling |
| Alerts | Real event table and authenticated snapshot dialog | Scoped events/evidence APIs |
| Personnel/Attendance | Existing capabilities remain available | Cloud personnel sync and real attendance events |

Keep `.sidebar`, `.nav-menu`, `.main`, `.content`, camera tile classes and existing style.css as a consistent shell. main.js handles authenticated requests, timeouts and UI hydration. Do not copy stored tokens or camera secrets into HTML.

Local edge URLs are **not public cloud streams**:

```text
/api/v1/cameras/{id}/stream
/api/v1/cameras/{id}/tracking-stream
```

Only the shop PC can serve these loopback routes. Cloud live is inventory/health, with secure streaming unavailable until a validated edge -> WebRTC/relay -> browser transport exists. Do not proxy private RTSP through the browser or expose port 554.

## Camera Contract

`settings.tracking_mode`: track enables persistent ByteTrack, detect disables tracking IDs. Never map Person Tracking to unknown_detection. Features use attendance, face_recognition, unknown_detection, shoplifting and object_security; compatibility aliases are normalized. Crowd thresholds are separate, not a shoplifting flag.

Performance settings: tracking_fps 1..12, tracking_imgsz 256..640, tracking_quality numeric 35..95. Effective runtime profile may cap these further. Legacy balanced quality normalizes to 65. Role ENTRANCE_EXIT runs line-crossing attendance when attendance is enabled; GENERAL/SECURITY can emit unknown alarms when configured. Zone labels normalize to existing inside/outside semantics. enabled=false stops the worker. Browser camera views omit stored source credentials; __KEEP_EXISTING__ preserves a source, edge-local:{id} adopts a saved edge source without exposing it.

## Events And CRM Mapping

Local business events are committed to SQLite and its outbox, then wrapped as edge.event.v1 with authenticated tenant/shop/site/edge, event_id, camera_id, event_type, event_time and payload. Network failure retries without stopping local AI. Cloud deduplicates by event ID. Image evidence is uploaded before its event references it; missing legacy files are marked unavailable rather than blocking attendance permanently. Alert clips remain local in this version.

Attendance entry/exit requires a recognized identity and an actual configured line crossing, not mere face visibility/disappearance. Crossing evidence/time is recorded at the crossing. Breaks require confirmed business actions; track loss does not automatically start a break. CRM employee/user mappings must be verified before enabling API delivery. Do not substitute our local person ID for a CRM user ID.

Relevant persistent tables are documented in CRM_INTEGRATION_ARCHITECTURE.md. Cloud portal events/attendance provide a working intermediate destination; later CRM API mapping must preserve canonical identity, UTC timestamps, event idempotency and snapshot linkage.

## Security And Rollout

Production uses PostgreSQL and scoped device credentials. Readiness, scoped activation, deletion commands, image evidence, enrollment and real data delivery must be tested on the deployment before rollout. Existing data is not migrated destructively. Filesystem admins can still read local data; encrypted-at-rest secret management and server-enforced subscription entitlements are separate hardening work, not claimed as completed here.


## Automatic face attendance and manual attendance controls

### Authoritative personnel source

Camera Eye Cloud reads the CRM face directory with:

`GET /api/User/face-embeddings/{tenantCode}`

The Camera Eye tenant identifier is the CRM tenant code (for example `ABM-46-775`). The directory response's per-user `tenantId` is the CRM tenant UUID and is the identifier required by CRM face login. Do not hardcode the UUID.

### Automatic entrance login

For an enabled `ENTRANCE_EXIT` camera, the edge performs local face recognition and saves the current recognized face crop. The durable edge queue uploads that image as event evidence before posting `PERSON_RECOGNIZED`.

Camera Eye Cloud then calls CRM:

`POST /api/Auth/loginUsingFaceTenant`

```json
{
  "base64Image": "<raw JPEG base64 from the current camera recognition>",
  "tenantId": "<CRM tenant UUID from the face directory>"
}
```

The stored CRM profile image is not used for production attendance. Camera Eye records `ATTENDANCE_ENTRY` only after the CRM face-login request succeeds. The first valid recognition is sent immediately; duplicate successful attendance for the same person/day is suppressed.

### CRM frontend / manual controls

The CRM frontend may use the existing Camera Eye portal API surface after creating a scoped CRM session:

- `GET /portal/v1/tenants/{tenant_id}/personnel` — CRM-synced personnel.
- `GET /portal/v1/tenants/{tenant_id}/attendance` — attendance records, current presence and person events.
- `GET /portal/v1/tenants/{tenant_id}/attendance-station/candidate?camera_id=...&edge_id=...` — fresh recognized candidate.
- `POST /portal/v1/tenants/{tenant_id}/attendance-station/action` — explicit `CHECK_IN`, `CHECK_OUT`, `BREAK_START`, or `BREAK_END`.
- `POST /portal/v1/tenants/{tenant_id}/attendance-station/live/start` and `/live/stop` — attendance-camera WebRTC viewing.
- `GET /portal/v1/tenants/{tenant_id}/crm/status` — CRM integration status.

Manual actions remain explicit user operations and are intentionally separate from automatic `loginUsingFaceTenant`. This lets the CRM team expose only the controls required by its UI without changing the edge recognition pipeline.

### Security and failure behavior

CRM credentials stay in Camera Eye Cloud and are never sent to the edge or browser. RTSP credentials remain edge-local. Face Base64 is never written to application logs. If CRM face login fails or rejects the request, Camera Eye retains the recognition event for diagnostics but does not create a successful automatic attendance entry.

### Authoritative attendance roster

SnapKey CRM remains the business source of truth for monthly attendance and roster state. Camera Eye events remain the AI audit/evidence layer.

The CRM frontend should read official attendance through Camera Eye:

`GET /portal/v1/tenants/{tenant_id}/attendance/roster?year={YYYY}&month={M}&user_id={CRM_USER_UUID}`

The request uses the normal Camera Eye portal session:

`Authorization: Bearer <CAMERA_EYE_SESSION_TOKEN>`

`user_id` is optional. When omitted or empty, Camera Eye forwards an empty `userId` to the CRM roster endpoint. The CRM response is returned without inventing a second attendance model, preserving fields such as scheduled start/off time, actual start/off time, status, holiday/week-off/leave state, overtime, login/logout location, counter, and remarks.

Camera Eye calls CRM server-side:

`GET /api/UserRoster/GetUsersRoster?year={YYYY}&month={M}&userId={CRM_USER_UUID_OR_EMPTY}`

Do not call the CRM proxy URL directly from Camera Eye browser code and do not expose the CRM bearer token.

Use the two attendance surfaces for different purposes:

- `/attendance/roster` — official CRM attendance/calendar/history.
- `/attendance` and `/events` — Camera Eye recognition, camera events, evidence, and AI audit trail.

Automatic attendance remains: entrance recognition -> current face crop -> CRM `loginUsingFaceTenant` -> successful CRM attendance -> Camera Eye audit event.
