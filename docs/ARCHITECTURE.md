# Production Architecture

This product should run as a hybrid system:

```text
Client shop PC = local AI edge agent
Our platform = login UI, central database, alerts, reports, remote monitoring
```

The camera/video/model workload must stay local at each client site because RTSP streams are heavy, private, and unreliable over the public internet. The cloud should receive events, snapshots, short clips, health status, and summaries.

## Main Components

### 1. Edge Agent

Runs on the client's local PC.

Responsibilities:

```text
Connect RTSP/USB cameras
Run YOLO tracking locally
Run face recognition locally
Detect known staff, unknown persons, crowd alerts, break events
Save local evidence when internet is down
Sync events/snapshots/clips to cloud when online
Expose local fallback UI for setup/debug
```

Current repo is the start of this edge agent.

### 2. Cloud Platform

Runs on our server.

Responsibilities:

```text
User login
Client/shop/camera/staff configuration
Dashboard for live status
Attendance, break, and alert reports
Central event database
Evidence storage for snapshots and clips
WhatsApp/SMS/email alert delivery
Remote configuration updates to edge agents
```

The cloud should not pull full camera streams by default. It should show live/tracking only through secure on-demand relay or local network access.

### 3. Sync Service

Edge agent sends structured events to cloud.

Event examples:

```json
{
  "event_id": "uuid",
  "tenant_id": "client_001",
  "site_id": "shop_001",
  "camera_id": "billing_counter",
  "event_type": "UNKNOWN_INSIDE_ALERT",
  "event_time": "2026-08-22T10:30:00Z",
  "severity": "HIGH",
  "person_id": null,
  "track_id": "17",
  "metadata": {
    "person_count": 3,
    "camera_zone": "inside"
  },
  "snapshot_url": "cloud/evidence/...",
  "clip_url": "cloud/evidence/..."
}
```

Sync rules:

```text
Every event gets a UUID.
Events are queued locally first.
Cloud upload is retryable.
Duplicate uploads are ignored by event_id.
Snapshots/clips upload after event metadata.
If internet is down, local agent keeps working.
```

## Camera Types

Each camera must be configurable:

```text
Camera name
RTSP/source URL
Zone: inside or outside
Role: entrance, billing counter, shelf, outside queue, store room
Features enabled: tracking, face recognition, unknown alerts, crowd alerts, attendance
Crowd threshold
Alert recipients
Clip duration
```

Inside cameras:

```text
Recognize staff/owners/managers
Alert on unknown persons
Track break/removal from view
Capture evidence snapshots
```

Outside cameras:

```text
Show total person count
Alert on crowd/queue threshold
Usually do not alert unknown person by face
```

## Attendance And Breaks

Required records:

```text
Person name
Role: owner, manager, worker
Entry time
Exit time
Last seen time
Camera name
Break start time
Break end time
Snapshot evidence
Confidence
```

Attendance is created when a known face is recognized. Entry/exit line crossing is optional but recommended for entrance cameras.

Break behavior:

```text
Known person disappears from camera view -> BREAK_START
Known person appears again -> BREAK_END
```

This should not be confused with official store exit unless the entrance/exit camera line confirms it.

## Alerts

Alert types:

```text
UNKNOWN_INSIDE_ALERT
CROWD_ALERT
LONG_BREAK_ALERT
AFTER_HOURS_MOVEMENT
CAMERA_OFFLINE
CAMERA_DEGRADED
SYNC_OFFLINE
```

Alert channels:

```text
In-app banner
WhatsApp message
SMS/email later if required
```

WhatsApp messages should be short:

```text
Alert: Unknown person detected inside Madghusala Store.
Camera: Billing Counter
Time: 10:31 AM
Snapshot: <link>
```

Use official WhatsApp Business/Cloud API or an approved BSP. Do not depend on unofficial WhatsApp Web automation for production.

## Evidence

Store:

```text
Arrival snapshot
Exit snapshot
Break start snapshot
Break end snapshot
Unknown person snapshot
Crowd snapshot
Optional 5-15 second clip
```

Local edge agent writes evidence first. Cloud upload happens async.

Recommended storage:

```text
Local: data/evidence/
Cloud DB: event metadata
Cloud object storage: snapshots/clips
```

## Security

Minimum production requirements:

```text
Tenant/site separation
User login with roles
Encrypted API tokens between edge and cloud
Do not expose RTSP passwords in cloud UI
Do not stream cameras publicly without signed access
Audit logs for alert acknowledgement
Config backups
```

User roles:

```text
Owner: full access
Manager: attendance and alerts
Operator: live/tracking only
Support: device health and logs only
```

## Modular Design

Keep modules separate:

```text
camera_sources/     RTSP, USB, file, vendor SDK later
detectors/          YOLO models and future custom bottle/shoplifting models
trackers/           ByteTrack now, replaceable later
identity/           InsightFace now, replaceable later
rules/              unknown, crowd, break, after-hours, line crossing
alerts/             in-app, WhatsApp, email, SMS
sync/               cloud upload and retry queue
storage/            local SQLite now, cloud Postgres later
ui/                 local setup UI and cloud dashboard
```

Rule modules should be data-driven so client-specific changes do not require changing core tracking.

Example:

```yaml
rules:
  crowd:
    enabled: true
    threshold: 10
    camera_zone: outside
  unknown_inside:
    enabled: true
    camera_zone: inside
  long_break:
    enabled: true
    minutes: 15
```

## Development Phases

### Phase 1: Tomorrow Demo

```text
Run edge agent locally
Use setup UI
Add cameras
Add personnel and face images
Show LIVE and TRACKING
Show attendance/break events
Show unknown/crowd alerts
```

### Phase 2: Client Pilot

```text
Add edge agent ID
Add local event queue
Add cloud sync endpoint
Add cloud login dashboard
Upload snapshots to cloud
Send WhatsApp alerts
```

### Phase 3: Production SaaS

```text
Multi-tenant cloud dashboard
Device monitoring
Remote config management
Role-based access
Billing/subscription
Model/version management
Central reports and exports
```

## What Not To Do

Avoid:

```text
Sending all RTSP video to cloud
Hardcoding client-specific rules
Making WhatsApp alerting block video processing
Mixing UI, AI, alerts, and sync in one file
Treating "person not visible" as final exit
Using one global model/config for every client
```

## Current Edge Flow

Current local flow:

```text
Camera source -> YOLO tracking -> face recognition -> presence/break events -> snapshots -> local UI
```

Next production step:

```text
Local event queue -> cloud sync API -> platform dashboard -> WhatsApp alert delivery
```
