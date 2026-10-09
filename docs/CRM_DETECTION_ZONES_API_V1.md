# Camera Eye detection zones API v1

This document describes server-to-server APIs for configuring unknown-person detection areas and retrieving confirmed incidents. The APIs are versioned under `/integration/v1`; they do not expose the Windows edge credential to a browser.

## Authentication and scope

Send `X-CRM-Integration-Key: <server-side key>` from the CRM backend. Keep the key in a server secret store; never send it to a browser. The URL identifies `tenant_id`, `shop_id`, `edge_id`, and canonical `camera_id`. The cloud service verifies that the camera belongs to all four identifiers. A mismatch is rejected. CRM reads and writes must include `edge_id` as a query parameter. The portal also offers the same read and full-replacement operation using its existing Bearer portal session, and checks the session tenant/shop plus administrator role for writes.

Base URL is the deployed Camera Eye cloud origin. Example below uses `https://camera.snapkey.ai`.

## Modes and version behavior

`FULL_FRAME` monitors normalized `{x:0,y:0,width:1,height:1}` without storing a synthetic zone row. It is the default for a newly registered camera with unknown-person detection enabled. `CUSTOM_ZONES` monitors only enabled rectangles. An empty custom list deliberately disables zone matching. Turning the camera's unknown-detection feature off is independent and always disables the effective detector.

Every saved configuration has a monotonically increasing `version`. Include the version last read as `expected_version`; stale writes return `409` and must be refreshed before retry. Repeating an unchanged payload is idempotent and does not increment the version. The cloud marks changes `PENDING`; edge polls `/edge/v1/config/cameras`, persists the configuration locally, and posts an acknowledgement. Offline detection continues from the SQLite cache. A local edge override is reported as `LOCAL_OVERRIDE` and cloud settings are not silently substituted over it.

## Retrieve configuration and sync status

`GET /integration/v1/tenants/{tenant_id}/shops/{shop_id}/cameras/{camera_id}/detection-config?edge_id={edge_id}`

Example response:

```json
{
  "camera_id": "camera-16bf318d-589a-4f21-92ce-534f7f08f79b",
  "mode": "FULL_FRAME",
  "zones": [],
  "version": 2,
  "applied_version": 2,
  "sync_status": "APPLIED",
  "local_override": false,
  "effective_mode": "FULL_FRAME",
  "effective_zones": [{"id":"FULL_FRAME","name":"Full frame","x":0,"y":0,"width":1,"height":1,"enabled":true}]
}
```

`sync_status` is `DEFAULT`, `PENDING`, `APPLIED`, `LOCAL_OVERRIDE`, or `FAILED`. `applied_version` is the version last acknowledged by the assigned edge.

## Replace the complete configuration

`PUT /integration/v1/tenants/{tenant_id}/shops/{shop_id}/cameras/{camera_id}/detection-config?edge_id={edge_id}`

Headers: `X-CRM-Integration-Key`, `Content-Type: application/json`.

```json
{
  "mode": "CUSTOM_ZONES",
  "expected_version": 2,
  "zones": [
    {"id":"entrance-left","name":"Entrance left","x":0.0,"y":0.05,"width":0.48,"height":0.9,"enabled":true},
    {"id":"entrance-right","name":"Entrance right","x":0.52,"y":0.05,"width":0.48,"height":0.9,"enabled":false}
  ]
}
```

To restore full frame, send `{"mode":"FULL_FRAME","expected_version":3,"zones":[]}`. To explicitly disable all custom areas, send `CUSTOM_ZONES` with an empty array.

## Create, update, and delete one custom zone

These convenience operations use the same config version and validation. They return the complete effective config. Zone IDs are unique within one camera.

- `POST /integration/v1/tenants/{tenant_id}/shops/{shop_id}/cameras/{camera_id}/detection-zones?edge_id={edge_id}` with one zone plus `expected_version`.
- `PUT /integration/v1/tenants/{tenant_id}/shops/{shop_id}/cameras/{camera_id}/detection-zones/{zone_id}?edge_id={edge_id}` with one zone (matching path ID) plus `expected_version`.
- `DELETE /integration/v1/tenants/{tenant_id}/shops/{shop_id}/cameras/{camera_id}/detection-zones/{zone_id}?edge_id={edge_id}&expected_version={version}`.

Example create body:

```json
{"id":"entrance","name":"Entrance","x":0.1,"y":0.05,"width":0.8,"height":0.9,"enabled":true,"expected_version":2}
```

All coordinates are floating-point normalized values within `[0,1]`; width and height must be greater than zero and the rectangle must fit inside the frame. Maximum 64 zones; ID length 1–128; name length 1–80.

## Incident retrieval

`GET /integration/v1/tenants/{tenant_id}/shops/{shop_id}/unknown-incidents?edge_id={edge_id}&camera_id={camera_id}&limit=100`

`edge_id` and `camera_id` filters are optional. `limit` is 1–500. Results are tenant/shop scoped and include the canonical current `camera_id`; historic events for `webcam_1` remain unchanged and are not reassigned.

```json
{"items":[{"event_id":"evt-123","tenant_id":"tenant-a","shop_id":"shop-a","edge_id":"edge-a","camera_id":"camera-16bf318d-589a-4f21-92ce-534f7f08f79b","event_time":"2026-10-08T10:00:00+00:00","event_type":"UNKNOWN_INCIDENT","incident":{"metadata":{"evidence_status":"PARTIAL"}}}]}
```

## Errors

`400` malformed scope, `401` invalid integration key, `403` unauthorized portal scope, `404` camera/zone not found in the requested scope, `409` stale `expected_version`, `422` invalid mode/zone/coordinates/limit, and `500` unexpected cloud failure. On `409`, GET the latest config and reapply the intended change; do not retry an old version blindly.

## cURL example

```sh
curl -X PUT 'https://camera.snapkey.ai/integration/v1/tenants/tenant-a/shops/shop-a/cameras/camera-123/detection-config?edge_id=edge-a' \
  -H 'X-CRM-Integration-Key: <server-side-secret>' -H 'Content-Type: application/json' \
  --data '{"mode":"FULL_FRAME","zones":[],"expected_version":0}'
```
