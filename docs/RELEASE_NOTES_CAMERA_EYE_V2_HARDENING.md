# Camera Eye V2 hardening release notes

## Changes in this branch

- Person-wise attendance reporting clips work and explicit break periods to a selected IANA-timezone business day, includes preceding-day activity for overnight shifts, and provisionally counts open sessions through the current time.
- V2 CRM integration requests can be restricted by a production tenant/shop allow-list. Production refuses CRM integration access when the allow-list is missing.
- V2 CRM auto-logout requires a full 60-minute absence and healthy coverage from the person’s last attendance camera. A stale in-flight remote mutation is not automatically replayed; ambiguous outcomes require CRM reconciliation.
- Added mocked CRM-adjacent safety regressions and mocked SMTP/WhatsApp adapter tests. No real CRM mutation or notification was sent.
- Added the secret-free environment example, staging/backup/rollback runbook, and updated V2 contract status docs.

## Not included / release blockers

- No production deployment, merge, push, or installer release was performed.
- No staging PostgreSQL, actual CRM contract, Windows packaging, or client-site validation was available in this run.
- V2 evidence media delivery remains manifest-only; action-linked three-snapshot/clip capture and missing-evidence reasons are incomplete.
- Notifications remain shop-wide synchronous sends; durable outbox, retries, rate limiting, deduplication, per-person preferences, and status API remain outstanding.
- Paid/unpaid break classification and absence payroll deductions require an approved business rule.
- Postgres schema bootstrap uses inline idempotent DDL; no versioned migration framework or staging backup/restore validation exists.

## Feature flags

Keep `CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false`. Do not enable it until the staging tenant has verified identity matching, 60-minute conditions, CRM 200 response criteria, timeout reconciliation, and action audit history. The encryption key and production integration scope allow-list must be provisioned via the host secret manager.
