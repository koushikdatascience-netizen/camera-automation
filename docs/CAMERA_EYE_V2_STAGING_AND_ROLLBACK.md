# Camera Eye V2 staging and rollback runbook

Status: **staging preparation only**. This branch has not been deployed or verified against a staging PostgreSQL instance or the live CRM contract. Keep `CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false`. The separate legacy CRM flags are independent.

The production deploy workflow is manual-dispatch only and targets the GitHub `production` environment. Before any dispatch, a repository administrator must configure required reviewers under **Settings → Environments → production**. The GitHub environment currently has no protection rules, so the workflow file alone cannot require reviewer approval.

## Configuration

Copy `.env.cloud.example` to `.env` on the staging host and provision every secret using the host's secret manager. Never commit `.env`, CRM face crops, access tokens, or employee JWTs. Production CRM integration requires `SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES`, a JSON list of tenant/shop grants, e.g. `[{"tenant_id":"tenant-a","shop_ids":["shop-1"]}]`. Each V1/V2 integration route checks its URL tenant and shop against that list. The allow-list scopes the configured shared key; it is not a per-tenant credential. Requests and policy writes are logged with tenant/shop/user IDs, never the key.

`CAMERA_EYE_TOKEN_ENCRYPTION_KEY` must be a Fernet key before V2 face tokens can be cached. Generate one inside a trusted staging environment with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Back it up in the secret manager. Rotation requires a controlled maintenance window: stop V2 auto-logout, decrypt existing rows with the old key and re-encrypt with the new key in a one-time offline migration, validate token expiry/identity, deploy the new key, then restart. If the old key is lost, delete the cached encrypted tokens and let users re-enroll/re-authenticate; the key cannot be recovered. Do not configure both keys in a single process or log token plaintext.

## Staging deployment

From a clean checkout of the reviewed branch, provision `.env`, then run:

```powershell
docker compose -f docker-compose.cloud.yml config --quiet
docker compose -f docker-compose.cloud.yml pull postgres
docker compose -f docker-compose.cloud.yml up -d --build postgres
docker compose -f docker-compose.cloud.yml exec -T postgres pg_dump -U snapkey -d snapkey_vision -Fc -f /tmp/camera-eye-before.dump
$postgresContainer = docker compose -f docker-compose.cloud.yml ps -q postgres
docker cp "${postgresContainer}:/tmp/camera-eye-before.dump" .\camera-eye-before.dump
docker compose -f docker-compose.cloud.yml up -d --build portal
docker compose -f docker-compose.cloud.yml ps
docker compose -f docker-compose.cloud.yml logs --tail 200 portal
```

Replace database/user names in the backup command if `.env` overrides defaults. Confirm `/health`, the allowed and denied tenant/shop scope cases, legacy endpoints, V2 policy CRUD, activity paging, local-day summaries, alert reads, and evidence manifest behavior. Use a CRM mock/staging tenant only. Test face-token expiry and identity mismatch. Keep all real attendance mutation flags disabled until an approved end-to-end test with a designated test employee. This compose file currently publishes portal only on loopback; put the approved TLS reverse proxy in front before any external staging access.

## Database behavior and backup

`PostgresPortalStore._init()` applies idempotent `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, and index statements on portal startup. These are inline schema bootstraps, not a versioned migration system. This hardening adds `attendance_presence.last_camera_zone`, CRM action-recovery metadata, `notification_outbox`, and `attendance_camera_coverage`. Review DDL in `cloud_portal/postgres_storage.py` and take a tested PostgreSQL backup before upgrading. Verify a fresh database and one previous-release upgrade in staging; a SQLite test is not a substitute. `tests/test_postgres_attendance_integration.py` is enabled with `SNAPKEY_TEST_DATABASE_URL` pointed at a disposable database whose name contains `test`.

For a local database integration run, start Docker Desktop and run `tools/run_postgres_integration.ps1` from the repository root. It starts the opt-in `postgres-test` Compose service on loopback port 55432, runs the test against its isolated disposable database, and removes the test container afterward. It does not connect to or modify the portal's persistent `postgres` service. CI creates a disposable `camera_eye_test` database in its PostgreSQL service and treats a skipped database test as a failure.

Back up before each upgrade:

```powershell
docker compose -f docker-compose.cloud.yml exec -T postgres pg_dump -U snapkey -d snapkey_vision -Fc -f /tmp/camera-eye-before.dump
$postgresContainer = docker compose -f docker-compose.cloud.yml ps -q postgres
docker cp "${postgresContainer}:/tmp/camera-eye-before.dump" .\camera-eye-before.dump
```

Store the dump outside the database container, encrypted and access-restricted. Prove restore into a disposable database before calling the backup usable.

## Rollback

Stop V2 mutations first and restore the prior reviewed application revision/image. For a source checkout rollback, record the deployed SHA, then:

```powershell
git checkout <previous-reviewed-sha>
docker compose -f docker-compose.cloud.yml up -d --build portal
docker compose -f docker-compose.cloud.yml ps
docker compose -f docker-compose.cloud.yml logs --tail 200 portal
```

The current DDL is additive/idempotent, so the prior portal may run with newer tables; do not drop schema objects during application rollback. If data corruption or a schema incompatibility is confirmed, stop portal writes, restore the pre-upgrade dump to a separate database, update `SNAPKEY_DATABASE_URL` to that restored database, validate reads, then restart. Do not overwrite the only backup. No production deployment or rollback has been performed as part of this change.

## CRM contract in this branch

- `POST /api/Auth/loginUsingFaceTenant` sends `{ "base64Image": "<CRM-enrolled face image>", "tenantId": "<CRM tenant>" }`. It is the face-token refresh flow and is intended not to mutate attendance. The token is encrypted at rest; JWT `exp` is treated only as an expiry hint and is not cryptographically verified by Camera Eye.
- `POST /api/UserRoster/LoginLogout` uses the validated CRM user ID, a timezone-aware ISO 8601 `date` (including the effective policy offset), and `actualStartTime` as `HH:MM:SS` for login, or `actualOffTime` for ordinary checkout. Send the employee Face Login token raw in `Authorization` (no `Bearer` prefix). Confirm only HTTP success plus JSON `success: true`.
- `POST /api/UserActivity/auto-logout` is a distinct 60-minute absence action with `{ "userId": "<CRM user>", "remarks": "..." }`. It is behind `CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED`, requires 60 minutes, scoped healthy camera coverage, and an encrypted face token. Any ambiguous failure enters reconciliation-required state and is not automatically replayed.

These bodies come from the supplied integration contract. Exact live CRM HTTP status/body success semantics and behavior after network timeouts still require contract tests against a designated CRM staging tenant. Never manually replay an ambiguous mutation before CRM reconciliation.

## Known release gaps

- Attendance recognition evidence now captures/uploads up to three snapshots and a short clip, links those assets to attendance activities, and reports missing/interrupted reasons. Delayed absence checkout links last-seen evidence but cannot create footage for the past. Authenticated integration and portal media proxies are implemented; validate retention and edge uploads in staging.
- Notifications now use a PostgreSQL outbox with deduplicated per-recipient enqueue, retry/backoff, terminal failure state, and delivery counts. Recipient resolution still uses shop policy lists; per-person recipient routing and rate limiting remain outstanding. No real email or WhatsApp delivery was tested.
- Ambiguous CRM auto-logout outcomes require an operator to compare CRM state and use the scoped reconciliation endpoint. A verified automatic CRM status-query contract is not available, so automatic remote reconciliation is not claimed.
- Daily totals now clip work/break intervals to a local day and provisionally count open sessions. Payroll absence deduction and paid/unpaid break classification have no approved policy and are deliberately not inferred.
- No real staging PostgreSQL, CRM, SMTP, WhatsApp, Windows installer, or client-site test was run here. Do not mark production-ready on unit tests alone.
