# CRM Integration Architecture

## Purpose

This document explains how Madhushala Camera AI / SnapKey Vision AI stores data and how it should integrate with the CRM system.

The camera system is designed as a hybrid edge-cloud product:

```text
Client shop PC = local edge AI agent
Cloud portal   = central multi-tenant platform
CRM            = business master system
```

The CRM team should use the cloud portal/API as the integration point. The local SQLite database on each client PC is for offline AI operation and should not become the CRM master database.

## High-Level Architecture

```text
Camera / RTSP / Webcam
        |
        v
Local Edge App on client PC
        |
        |-- local AI detection
        |-- face recognition
        |-- attendance/break logic
        |-- unknown person alerts
        |-- security object alerts
        |-- local SQLite evidence/events
        |
        v
Edge Sync Queue
        |
        v
Cloud Portal: https://camera.snapkey.ai
        |
        |-- tenant/shop/camera config
        |-- central events
        |-- portal sessions
        |-- edge credentials
        |-- reporting/dashboard
        |
        v
CRM integration
```

## Identity Scope

All cloud-facing data is scoped using this hierarchy:

```text
tenant_id
  -> company_code
  -> shop_id
  -> site_id
  -> edge_id
  -> camera_id
```

Recommended CRM mapping:

| Camera AI Field | CRM Meaning |
|---|---|
| `tenant_id` | CRM company/client id |
| `company_code` | CRM company code, optional human-readable code |
| `shop_id` | CRM shop/branch code |
| `site_id` | Shop/site id. Can match `shop_id` if CRM has no separate site table |
| `edge_id` | Installed client PC/device id |
| `camera_id` | Camera id inside one shop/device |
| `person_id` | Camera AI personnel id, should map to CRM employee/user id where possible |

## Database Responsibilities

### CRM Database

The CRM should remain the master for:

```text
companies
shops/branches
users/employees
roles
subscription ownership
business reports
```

### Camera Cloud Database

The cloud portal should be the master for:

```text
edge devices
camera configuration
camera event history
portal sessions
edge credentials
camera health
AI event payloads
```

### Local Edge Database

The local PC database should be used for:

```text
offline operation
local personnel/face enrollment
attendance session cache
local event queue
local evidence paths
sync retry state
```

Do not depend on the local SQLite database for CRM reporting. It can be offline, client-controlled, or unsynced. The cloud `edge_events` table is the correct integration bridge.

## Local Edge Database

Local database path:

```text
C:\ProgramData\MadhushalaCameraAI\data\camera_automation.db
```

Source code:

```text
camera_service/storage.py
camera_service/camera_manager.py
```

### `personnel`

Stores people enrolled into the camera system.

| Column | Meaning |
|---|---|
| `id` | Camera AI person id |
| `employee_code` | Employee code, should map to CRM employee/user code |
| `full_name` | Person name |
| `role` | OWNER / MANAGER / WORKER or configured role |
| `phone` | Optional phone |
| `email` | Optional email |
| `active` | Whether person is active |
| `created_at` | Created timestamp |
| `updated_at` | Updated timestamp |

CRM mapping:

```text
CRM employees/users -> personnel
CRM employee_code   -> personnel.employee_code
```

### `face_profiles`

Stores face embeddings and face preview image paths.

| Column | Meaning |
|---|---|
| `id` | Face profile id |
| `person_id` | Linked `personnel.id` |
| `embedding_json` | Face embedding vector for recognition |
| `quality` | Enrollment quality score |
| `image_path` | Local face preview image path |
| `created_at` | Created timestamp |

CRM note:

Face embeddings should usually stay inside the camera system. CRM may store employee photos, but it should not need to understand or edit `embedding_json`.

### `attendance_sessions`

Stores attendance entry/exit sessions.

| Column | Meaning |
|---|---|
| `id` | Attendance session id |
| `person_id` | Linked person |
| `store_id` | Local store/shop id |
| `arrival_time` | First seen / entry timestamp |
| `exit_time` | Exit timestamp |
| `arrival_camera` | Camera id for arrival |
| `exit_camera` | Camera id for exit |
| `arrival_confidence` | Recognition confidence at arrival |
| `exit_confidence` | Recognition confidence at exit |
| `arrival_snapshot` | Local evidence snapshot path |
| `exit_snapshot` | Local evidence snapshot path |
| `status` | `OPEN` or `CLOSED` |
| `entry_confirmed` | Whether entry was confirmed by crossing logic |

CRM mapping:

```text
ATTENDANCE_ENTRY -> attendance_sessions.arrival_time
ATTENDANCE_EXIT  -> attendance_sessions.exit_time
```

Important:

`attendance_sessions` is local operational storage. CRM should consume attendance from cloud events after sync.

### `person_events`

Stores person-related events that are not always full attendance sessions.

| Column | Meaning |
|---|---|
| `id` | Event id |
| `person_id` | Linked person, nullable |
| `store_id` | Store/shop id |
| `camera_id` | Camera id |
| `event_type` | Event type |
| `event_time` | Timestamp |
| `metadata_json` | Extra event details |

Common event types:

```text
BREAK_START
BREAK_END
CROWD_ALERT
UNKNOWN_INSIDE_ALERT
SHOPLIFTING_WATCH
EXIT_WITHOUT_OPEN_SESSION
```

CRM break mapping:

| CRM Concept | Camera AI Event |
|---|---|
| Break begins | `BREAK_START` |
| Break ends | `BREAK_END` |
| Employee | `person_id` |
| Evidence | `metadata_json.snapshot_path` |
| Camera/location | `camera_id` |

### `unknown_incidents`

Stores unknown person incidents.

| Column | Meaning |
|---|---|
| `id` | Incident id |
| `store_id` | Store/shop id |
| `camera_id` | Camera id |
| `track_id` | Tracking id from live camera |
| `first_seen` | First seen timestamp |
| `confirmed_unknown_at` | Unknown confirmation timestamp |
| `last_seen` | Last seen timestamp |
| `recognition_attempts` | Face recognition attempts |
| `best_similarity` | Best face similarity score |
| `best_face_snapshot` | Face snapshot path |
| `best_person_snapshot` | Person/full-frame snapshot path |
| `clip_path` | Optional clip path |
| `status` | `OPEN` or `ACKNOWLEDGED` |
| `acknowledged_at` | Acknowledgement time |

CRM mapping:

```text
UNKNOWN_INCIDENT -> security/visitor alert
UNKNOWN_INSIDE_ALERT -> urgent inside-shop unknown person alert
```

### `security_alerts`

Stores security alerts, such as scissors/object detection.

| Column | Meaning |
|---|---|
| `id` | Alert id |
| `store_id` | Store/shop id |
| `camera_id` | Camera id |
| `alert_type` | Alert type |
| `object_label` | Detected object label, e.g. `scissors` |
| `confidence` | Detection confidence |
| `event_time` | Timestamp |
| `snapshot_path` | Evidence snapshot path |
| `clip_path` | Optional evidence clip path |
| `status` | `OPEN` or `ACKNOWLEDGED` |
| `acknowledged_at` | Acknowledgement timestamp |
| `metadata_json` | Extra detection details such as bounding box |

Main event type:

```text
SECURITY_OBJECT_ALERT
```

### `object_security_events`

Lower-level object detection history.

| Column | Meaning |
|---|---|
| `id` | Event id |
| `camera_id` | Camera id |
| `object_class` | Object class, e.g. `scissors` |
| `confidence` | Detection confidence |
| `track_id` | Track id if available |
| `detected_at` | Detection timestamp |
| `confirmed` | Whether temporal confirmation passed |
| `alert_sent` | Whether alert/alarm was triggered |
| `snapshot_path` | Evidence path |
| `model_version` | Model version |
| `metadata_json` | Extra object details |

### `edge_event_queue`

Local offline-first sync queue. Every important event is inserted here before being sent to cloud.

| Column | Meaning |
|---|---|
| `id` | Event id |
| `event_type` | Event type |
| `payload_json` | Full event payload |
| `status` | `PENDING` or `SYNCED` |
| `attempts` | Upload attempts |
| `last_error` | Last sync error |
| `created_at` | Created timestamp |
| `next_attempt_at` | Retry timestamp |
| `claimed_at` | Reserved for worker claim |
| `synced_at` | Sync completion time |

CRM should not read this directly. It is a local transport queue.

## Local Camera Configuration Tables

Source code:

```text
camera_service/camera_manager.py
```

### `cameras`

Stores local camera definitions.

Important columns:

```text
camera_id
name
source_type
rtsp_url
enabled
camera_role
camera_zone
crowd_threshold
tracking_fps
tracking_imgsz
tracking_quality
tracking_mode
features_json
created_at
updated_at
```

Common feature flags in `features_json`:

```json
{
  "attendance": true,
  "face_recognition": true,
  "unknown_detection": true,
  "unknown_person_detection": true,
  "shoplifting": false,
  "shoplifting_detection": false,
  "object_security": true
}
```

### `camera_status`

Stores runtime camera health.

Important columns:

```text
camera_id
state
online
last_frame_at
capture_fps
ai_fps
frames_received
frames_dropped
reconnect_count
last_error
```

## Cloud Portal Database

Production cloud database uses PostgreSQL when `SNAPKEY_DATABASE_URL` is configured.

Source code:

```text
cloud_portal/postgres_storage.py
cloud_portal/api.py
```

### `tenants`

Represents client/company.

| Column | Meaning |
|---|---|
| `id` | Tenant id |
| `name` | Tenant name |
| `created_at` | Created timestamp |

CRM mapping:

```text
CRM company/client -> tenants
```

### `sites`

Represents site/shop under a tenant.

| Column | Meaning |
|---|---|
| `id` | Site/shop id |
| `tenant_id` | Tenant id |
| `name` | Site name |
| `created_at` | Created timestamp |

CRM mapping:

```text
CRM shop/branch -> sites
```

### `edge_machines`

Represents installed client machine/device.

| Column | Meaning |
|---|---|
| `id` | Edge id |
| `tenant_id` | Tenant id |
| `site_id` | Site/shop id |
| `last_seen_at` | Last sync/heartbeat time |

CRM mapping:

```text
Installed PC at shop -> edge_machines
```

### `edge_credentials`

Stores scoped edge API tokens.

| Column | Meaning |
|---|---|
| `token_hash` | Hashed API token |
| `tenant_id` | Tenant id |
| `company_code` | Company code |
| `shop_id` | Shop id |
| `site_id` | Site id |
| `edge_id` | Edge machine id |
| `enabled` | Whether token is active |
| `created_at` | Created timestamp |

This table is required for secure edge sync. The raw token should not be stored, only the hash.

### `edge_heartbeats`

Stores latest device status.

| Column | Meaning |
|---|---|
| `tenant_id` | Tenant id |
| `site_id` | Site id |
| `edge_id` | Edge id |
| `received_at` | Latest heartbeat time |
| `status_json` | Runtime status JSON |

CRM can use this for device health dashboards.

### `edge_events`

Main cloud event table. This is the most important CRM integration table.

| Column | Meaning |
|---|---|
| `id` | Global event id |
| `tenant_id` | Tenant/client |
| `company_code` | Company code |
| `shop_id` | Shop/branch id |
| `site_id` | Site id |
| `edge_id` | Edge machine id |
| `store_id` | Store id from local edge |
| `camera_id` | Camera id |
| `event_type` | Event type |
| `event_time` | Event time from edge |
| `received_at` | Cloud received time |
| `payload_json` | Full event payload |

CRM should consume this table/API for:

```text
attendance
breaks
unknown person alerts
security alerts
crowd alerts
shoplifting watch events
```

### `camera_configs`

Stores cloud-managed camera configuration that edge machines pull.

| Column | Meaning |
|---|---|
| `tenant_id` | Tenant id |
| `company_code` | Company code |
| `shop_id` | Shop id |
| `site_id` | Site id |
| `edge_id` | Edge id |
| `camera_id` | Camera id |
| `name` | Camera display name |
| `source_type` | `rtsp`, `webcam`, or `file` |
| `source` | RTSP URL or camera source |
| `camera_role` | `GENERAL`, `ENTRANCE_EXIT`, `SECURITY`, `SHOPLIFTING` |
| `camera_zone` | `inside` or `outside` |
| `crowd_threshold` | Crowd count threshold |
| `enabled` | Camera enabled flag |
| `features_json` | Enabled feature flags |
| `settings_json` | Runtime camera settings |
| `created_at` | Created timestamp |
| `updated_at` | Updated timestamp |

CRM can expose/edit camera config through cloud APIs.

### `edge_commands`

Stores commands for edge machines.

Current command types:

```text
ONVIF_PROBE
CAMERA_TEST
```

Used when CRM/cloud wants the edge machine to test a camera or probe camera profiles.

### `portal_sessions`

Stores short-lived portal sessions created by CRM.

| Column | Meaning |
|---|---|
| `session_id` | Public session id |
| `token_hash` | Hash of secret portal token |
| `tenant_id` | Tenant id |
| `company_code` | Company code |
| `shop_id` | Shop id |
| `user_id` | CRM user id |
| `display_name` | CRM user name |
| `role` | Role, e.g. OWNER/MANAGER/USER |
| `created_at` | Created timestamp |
| `expires_at` | Expiry timestamp |

CRM should create portal sessions instead of sharing permanent tokens.

## Main Event Types

Current event types produced by edge:

| Event Type | Meaning | CRM Mapping |
|---|---|---|
| `ATTENDANCE_ENTRY` | Person entry/arrival confirmed | Attendance check-in |
| `ATTENDANCE_EXIT` | Person exit confirmed | Attendance check-out |
| `BREAK_START` | Recognized person disappeared from active camera | Break started |
| `BREAK_END` | Recognized person returned | Break ended |
| `UNKNOWN_INCIDENT` | Unknown person incident confirmed | Security incident |
| `UNKNOWN_INSIDE_ALERT` | Unknown person detected inside shop | Urgent security alert |
| `CROWD_ALERT` | People count exceeded threshold | Crowd/queue alert |
| `SHOPLIFTING_WATCH` | Person + item activity needs review | Suspicious activity |
| `SECURITY_OBJECT_ALERT` | Security object detected, e.g. scissors | Object/security alert |

## Event Payload Shape

Cloud receives edge events in this envelope:

```json
{
  "schema_version": "edge.event.v1",
  "edge_id": "edge-shop-001",
  "tenant_id": "tenant-001",
  "company_code": "MADHU001",
  "shop_id": "SHOP001",
  "site_id": "SHOP001",
  "event_id": "uuid",
  "event_type": "SECURITY_OBJECT_ALERT",
  "event_time": "2026-09-28T10:30:00+00:00",
  "store_id": "client-shop-01",
  "camera_id": "billing_counter",
  "payload": {
    "event_id": "uuid",
    "store_id": "client-shop-01",
    "camera_id": "billing_counter",
    "event_type": "SECURITY_OBJECT_ALERT",
    "event_time": "2026-09-28T10:30:00+00:00",
    "metadata": {
      "object_label": "scissors",
      "confidence": 0.88,
      "snapshot_path": "data/evidence/billing_counter/snap.jpg",
      "clip_path": "data/evidence/billing_counter/clip.mp4"
    }
  }
}
```

## CRM Portal Session Flow

CRM should open the camera portal using this flow:

```text
CRM user clicks "Camera AI"
        |
        v
CRM backend calls POST /crm/session
        |
        v
Camera cloud creates portal_sessions row
        |
        v
Camera cloud returns launchUrl
        |
        v
CRM opens launchUrl in browser
```

Endpoint:

```text
POST https://camera.snapkey.ai/crm/session
```

Required header:

```text
X-CRM-Integration-Key: <server-side integration key>
```

Request:

```json
{
  "tenantId": "tenant-001",
  "companyCode": "MADHU001",
  "shopCode": "SHOP001",
  "userId": "crm-user-123",
  "displayName": "Manager Name",
  "role": "MANAGER"
}
```

Response:

```json
{
  "sessionId": "session-id",
  "expiresAt": "2026-09-28T11:30:00+00:00",
  "launchUrl": "https://camera.snapkey.ai/portal?sessionId=session-id#session=secret-token"
}
```

The CRM should redirect/open the `launchUrl`.

## Camera Config Flow

Cloud/CRM can create or update camera assignments.

```text
CRM/cloud saves camera_configs
        |
        v
Edge sync worker calls GET /edge/v1/config/cameras
        |
        v
Local edge applies camera config
        |
        v
Tracking/attendance/alerts run locally
```

Portal endpoint:

```text
PUT /portal/v1/tenants/{tenant_id}/cameras/{camera_id}
```

Example config:

```json
{
  "tenant_id": "tenant-001",
  "company_code": "MADHU001",
  "shop_id": "SHOP001",
  "site_id": "SHOP001",
  "edge_id": "edge-shop-001",
  "camera_id": "entrance_01",
  "name": "Entrance Camera",
  "source_type": "rtsp",
  "source": "rtsp://user:pass@192.168.1.10:554/Streaming/Channels/102",
  "camera_role": "ENTRANCE_EXIT",
  "camera_zone": "inside",
  "crowd_threshold": 10,
  "enabled": true,
  "features": {
    "attendance": true,
    "face_recognition": true,
    "unknown_detection": true,
    "object_security": false
  },
  "settings": {
    "tracking_fps": 3,
    "tracking_imgsz": 384,
    "tracking_quality": 60,
    "tracking_mode": "detect"
  }
}
```

## Edge Sync Flow

```text
Local event created
        |
        v
Stored in local table
        |
        v
Stored in edge_event_queue
        |
        v
sync_worker sends event to /edge/v1/events
        |
        v
Cloud stores in edge_events
        |
        v
CRM/dashboard reads central event
```

Sync is retryable. If internet is down, local AI still works and events stay queued.

## Recommended CRM Integration Points

Use these APIs/tables:

```text
POST /crm/session
GET  /portal/v1/tenants/{tenant_id}/events
GET  /portal/v1/tenants/{tenant_id}/summary
GET  /portal/v1/tenants/{tenant_id}/cameras
PUT  /portal/v1/tenants/{tenant_id}/cameras/{camera_id}
POST /portal/v1/tenants/{tenant_id}/edge-commands
```

CRM should not directly query:

```text
local SQLite files on client PCs
face_profiles.embedding_json
edge_event_queue
RTSP passwords from local config
```

## Suggested CRM Table Mapping

| CRM Table | Camera AI Mapping |
|---|---|
| `companies` | `tenants` |
| `shops` / `branches` | `sites`, `shop_id` |
| `users` / `employees` | `personnel` |
| `attendance` | `edge_events` where `event_type` is `ATTENDANCE_ENTRY` / `ATTENDANCE_EXIT` |
| `breaks` | `edge_events` where `event_type` is `BREAK_START` / `BREAK_END` |
| `alerts` | `edge_events` where `event_type` is `UNKNOWN_INCIDENT`, `UNKNOWN_INSIDE_ALERT`, `SECURITY_OBJECT_ALERT`, `CROWD_ALERT` |
| `devices` | `edge_machines` |
| `cameras` | `camera_configs` |

## Recommended CRM Decisions

The CRM team should decide:

1. Whether CRM employee id should become `personnel.id` or stay as `employee_code`.
2. Whether `shop_id` and `site_id` are the same value.
3. Which CRM roles map to camera portal roles: `OWNER`, `MANAGER`, `USER`, `SUPPORT`.
4. Whether CRM stores attendance after sync, or reads live from `edge_events`.
5. Whether alert acknowledgement should happen in CRM, camera portal, or both.
6. How long evidence snapshots/clips should be retained.
7. Whether camera RTSP credentials are stored in CRM or only in the camera portal.

## Important Production Rule

Keep the systems separated:

```text
CRM = business master data
Camera AI = local AI and event generation
Cloud edge_events = integration bridge
```

This makes the product modular, offline-capable, and easier to adapt to future industries.
