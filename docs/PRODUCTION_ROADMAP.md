# Production Roadmap

## Immediate Demo Scope

Keep the current system focused and stable:

```text
Local camera setup
Live preview
Tracking preview
Personnel enrollment
Known name overlay
Attendance presence
Break events
Unknown alerts
Crowd alerts
Evidence snapshots
```

## Next Build: Edge-To-Cloud

Add these modules without changing the existing camera/tracking code:

```text
edge_identity.py       Stores edge_id, tenant_id, site_id, auth token
event_queue.py         Local SQLite queue for events waiting to sync
cloud_client.py        Sends events/snapshots/clips to platform API
alert_dispatcher.py    Sends WhatsApp/SMS/email through configured providers
rules_engine.py        Runs configurable rules like crowd, long break, after-hours
```

## Cloud Platform API

Minimum endpoints:

```text
POST /edge/v1/events
POST /edge/v1/evidence
GET  /edge/v1/config
POST /edge/v1/health
POST /edge/v1/heartbeat
```

Cloud dashboard endpoints:

```text
GET /dashboard/sites
GET /dashboard/cameras
GET /dashboard/events
GET /dashboard/attendance
GET /dashboard/evidence/{id}
POST /dashboard/alerts/{id}/acknowledge
```

## WhatsApp Alert Flow

```text
Local rule creates alert event
Edge uploads event + snapshot
Cloud stores event
Cloud sends WhatsApp message with snapshot link
Owner opens link after login
Owner acknowledges alert
```

Do not send WhatsApp directly from the video loop. Alerts must be async.

## Customization Points

Per client:

```text
Shop/site name
Camera zone and role
Crowd threshold
Unknown alert on/off
Long break minutes
After-hours schedule
Alert phone numbers
Clip length
Model version
Recognition threshold
```

Per camera:

```text
Inside/outside
Entrance/billing/shelf/queue/store room
Line crossing coordinates
Alert rules enabled
Detection classes enabled
Evidence retention days
```

## Simple Valuable Features To Add Next

1. Daily summary dashboard.
2. CSV export for attendance and alerts.
3. Long break alert.
4. After-hours movement alert.
5. Camera offline alert.
6. Evidence image preview instead of only file path.
7. Alert acknowledge notes.
8. Remote config sync.
9. Edge heartbeat status.
10. Cloud login dashboard.

## Production Rule

Every new feature should follow this pattern:

```text
Detector produces facts
Rules convert facts into events
Events are stored locally
Sync uploads events to cloud
Alerts are sent asynchronously
UI reads from event/report APIs
```

This keeps future changes safe and modular.
