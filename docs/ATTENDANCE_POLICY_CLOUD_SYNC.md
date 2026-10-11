# Cloud-to-edge attendance policy synchronization

## Authority and precedence

`CAMERA_EYE_CLOUD_POLICY_SYNC_ENABLED` is a cloud-server feature gate and defaults to `0`. With the gate off, the edge continues using its existing local SQLite policy. With the gate on, a new edge fetches a complete policy snapshot over its existing scoped Bearer edge credential during each normal synchronization cycle. The endpoint is read-only and never enqueues or executes an attendance or CRM action.

After a valid snapshot is accepted, the complete Cloud Portal policy is authoritative for shop defaults and mapped CRM employees. Employee snapshots are resolved in the cloud as explicit employee value, then shop value, then system default. Local policy rows are retained unchanged as the offline/rollback fallback; they are not overwritten. Local policy edits are rejected while a cloud snapshot is active and the UI reports each effective field as cloud-sourced. For local-only personnel, an existing local employee override remains layered over the cloud shop policy. An explicit disabled response turns cloud precedence off without deleting the saved cloud snapshot. A timeout or invalid response leaves the current effective policy unchanged.

## Synchronization and identity checks

`GET /edge/v1/config/attendance-policy` requires an active, non-legacy edge credential. Tenant, shop, and edge IDs come from that credential, never from client query parameters. The response contains a monotonic shop revision, timezone-aware update timestamp, a complete validated shop policy, and one effective policy per active CRM mapping (`local_person_id` plus `crm_user_id`). Mapping and policy writes advance the revision in the same PostgreSQL transaction. Inactive personnel are omitted; removal of an override is represented by the employee's newly resolved shop-inherited policy.

The Windows edge validates the scope, timestamp, revision, complete policy shape, threshold relationships, IANA timezone, unique identities, and the local CRM/tenant/shop metadata before applying anything. SQLite replaces the active shop snapshot, employee snapshots, content hash, revision receipt, and applied timestamp in one transaction. A repeated identical revision is idempotent; an older revision is rejected; a changed payload with the same revision is rejected. The attendance engine and attendance-mode selector read the accepted snapshot. Attendance sessions, evidence, local operator policy rows, personnel, and faces are not part of the transaction.

## Rollout and rollback

1. Back up PostgreSQL and the cloud `.env` using the existing production release runbook. Build and deploy the cloud image containing this additive migration and endpoint with `CAMERA_EYE_CLOUD_POLICY_SYNC_ENABLED=0`. Do not change the CRM attendance feature flags.
2. Confirm cloud health and that edge credentials are tenant/shop/edge scoped. Portal policy edits still work while the feature gate is off.
3. Release the compatible Windows EXE through the normal installer channel. Old EXEs ignore the new endpoint; they keep existing behavior.
4. Pilot one edge with a test shop policy, validate its policy revision/scope in local System Status/API and its effective check-in mode, then set the cloud flag to `1` for the intended rollout. This setting is global for this portal process; do not enable it until pilot acceptance.
5. For application rollback, restore the previous portal image and prior cloud `.env`, keeping the additive revision table. Set `CAMERA_EYE_CLOUD_POLICY_SYNC_ENABLED=0` before restarting the prior image. New EXEs that receive the disabled response revert to their preserved local SQLite policy; offline EXEs retain their last accepted cloud policy until a later sync response. Restoring the previous EXE also returns to its existing local-only policy reader. Do not drop the additive PostgreSQL table or edit attendance records as part of rollback.

This repository change does not push or deploy the image, install an EXE, or activate the feature gate.
