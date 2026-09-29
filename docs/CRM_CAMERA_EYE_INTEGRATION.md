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
