# SnapKey Vision AI Edge Cloud Sync MVP

## Product Decision

Detection, tracking, face recognition, attendance, unknown-person checks, crowd counting, object security alarms, snapshots, and clips stay on the client machine. The cloud receives results, not raw live video, so low-end PCs keep working locally and the SaaS portal can show history, alerts, reports, and mobile notifications.

## Edge Responsibilities

- Run RTSP/webcam detection locally.
- Draw live boxes locally for the client demo screen.
- Save attendance, breaks, unknown incidents, crowd alerts, object alerts, snapshots, and clip paths to local SQLite.
- Queue every important event in `edge_event_queue`.
- Retry cloud sync in the background when internet and activation are available.

## Cloud Responsibilities

- Authenticate each edge machine by `tenant_id`, `site_id`, `edge_id`, and API token.
- Store received events per tenant.
- Show multi-camera dashboards, alert history, attendance reports, and mobile views.
- Send WhatsApp/push/email notifications from the cloud.
- Keep tenant data fully separated.

## Event Envelope

`POST /edge/v1/events`

```json
{
  "schema_version": "edge.event.v1",
  "tenant_id": "tenant-demo",
  "site_id": "client-shop-01",
  "edge_id": "edge-client-shop-01",
  "event_id": "uuid",
  "event_type": "SECURITY_OBJECT_ALERT",
  "event_time": "2026-08-28T09:00:00+00:00",
  "store_id": "client-shop-01",
  "camera_id": "jewel_cam",
  "payload": {
    "event_id": "uuid",
    "store_id": "client-shop-01",
    "camera_id": "jewel_cam",
    "event_type": "SECURITY_OBJECT_ALERT",
    "event_time": "2026-08-28T09:00:00+00:00",
    "metadata": {
      "object_label": "scissors",
      "confidence": 0.88,
      "snapshot_path": "data/evidence/jewel/snap.jpg",
      "clip_path": "data/evidence/jewel/clip.mp4"
    }
  }
}
```

## Current Edge Events

- `ATTENDANCE_ENTRY`
- `ATTENDANCE_EXIT`
- `BREAK_START`
- `BREAK_END`
- `UNKNOWN_INCIDENT`
- `UNKNOWN_INSIDE_ALERT`
- `CROWD_ALERT`
- `SHOPLIFTING_WATCH`
- `SECURITY_OBJECT_ALERT`

## Configuration

```yaml
edge:
  edge_id: edge-client-shop-01
  tenant_id: tenant-demo
  site_id: client-shop-01
  activation_required: true
  activation_token: ""
  license_cache_path: data/license_cache.json
  license_public_key: ""

cloud_sync:
  enabled: false
  base_url: https://your-platform.example.com
  api_token: CHANGE_ME
  timeout_seconds: 10
  batch_size: 50
  interval_seconds: 15
```

## Subscription License Gate

The edge app accepts a server-issued signed license from `POST /api/v1/license/install`. The cached license controls:

- expiry date and grace period
- tenant, site, edge, and machine binding
- enabled features
- maximum camera count
- limited mode after expiry or tampering

When limited mode is active, the API blocks camera start/tracking and premium features with HTTP `402`. Existing local data remains on the machine, but the business value is restricted because cloud sync, alerts, attendance reports, multi-camera operation, and premium modules depend on a valid license.

Development signing endpoint:

`POST /portal/v1/licenses/issue`

Keep `SNAPKEY_LICENSE_PRIVATE_KEY` only on the cloud server. The client EXE should contain only `license_public_key` or receive it through trusted configuration. The edge verifies Ed25519 signatures locally, so it can enforce expiry and grace without internet while still being unable to create licenses.

Generate a production key pair:

```powershell
.\.venv311\Scripts\python.exe tools\generate_license_keys.py
```

Use the private key on the portal and the public key in the edge configuration.

## API Endpoints

- `GET /api/v1/edge/status`: local queue, license, sync worker, alert/evidence status.
- `POST /api/v1/edge/sync`: manually drain one batch to cloud.

## Portal Build Order

1. Tenant, site, camera, and edge-machine tables. Current MVP: `cloud_portal/storage.py`.
2. Edge API endpoint accepting `edge.event.v1`. Current MVP: `POST /edge/v1/events`.
3. Alert and attendance dashboards filtered by tenant/site/camera. Current MVP APIs: `/portal/v1/tenants/{tenant_id}/summary` and `/portal/v1/tenants/{tenant_id}/events`.
4. Evidence upload service for snapshots/clips.
5. Mobile-friendly portal and notification rules.
