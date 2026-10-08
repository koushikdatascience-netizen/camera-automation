# Camera Eye V2 hardening release notes

## Changes in this branch

- Person-wise attendance reporting clips work and explicit break periods to a selected IANA-timezone business day, includes preceding-day activity for overnight shifts, and provisionally counts open sessions through the current time.
- V2 CRM integration requests can be restricted by a production tenant/shop allow-list. Production refuses CRM integration access when the allow-list is missing.
- V2 CRM auto-logout requires a full 60-minute absence and healthy coverage from the person’s last attendance camera. A stale in-flight remote mutation is not automatically replayed; ambiguous outcomes require CRM reconciliation.
- Person-specific absence thresholds are independent of the CRM endpoint's fixed 60-minute contract. MANUAL check-ins remain monitored; camera outages report UNKNOWN coverage and cannot trigger absence actions.
- CRM-confirmed logout has a recoverable local-finalization state and stable attendance action ID. An integration reconciliation endpoint lets CRM operations record a verified upstream outcome after an ambiguous timeout.
- Notifications use a durable PostgreSQL outbox with per-recipient deduplication, worker claims, retries/backoff, terminal failure, and delivery counts.
- Edge recognition now captures up to three snapshots and a short clip, uploads them to scoped storage, and links their manifest and missing reasons to attendance activities.
- Added regression tests and a PostgreSQL integration test gated by `SNAPKEY_TEST_DATABASE_URL`. No real CRM mutation or notification was sent.
- Added the secret-free environment example, staging/backup/rollback runbook, and updated V2 contract status docs.

## Not included / release blockers

- No production deployment, merge, push, or installer release was performed.
- No staging PostgreSQL, actual CRM contract, Windows packaging, or client-site validation was available in this run.
- Staging validation remains required for PostgreSQL recovery/outbox behavior, camera-zone coverage, edge evidence capture/media access, and the CRM reconciliation contract. Per-person notification recipient routing and rate limiting remain outstanding.
- Paid/unpaid break classification and absence payroll deductions require an approved business rule.
- Postgres schema bootstrap uses inline idempotent DDL; no versioned migration framework or staging backup/restore validation exists.

## Feature flags

Keep `CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false`. Do not enable it until the staging tenant has verified identity matching, 60-minute conditions, CRM 200 response criteria, timeout reconciliation, and action audit history. The encryption key and production integration scope allow-list must be provisioned via the host secret manager.
