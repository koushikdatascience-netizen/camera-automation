# Camera Eye manual-attendance MVP production handoff — 2026-10-10

## Release and approval status

Source branch: `codex/crm-policy-alerts-finalization-20261010`. Baseline:
`a531eb215303c528973753def23a59639421dc73`. The final release is the commit
containing this document; obtain its complete immutable SHA with
`git log -1 --format=%H -- docs/CAMERA_EYE_PRODUCTION_HANDOFF.md`.

Production deployment NOT STARTED. No live attendance mutations are authorized.
STOP before production bootstrap/migration, credential binding, container replacement,
or live feature-flag changes. The user explicitly requires approval for these writes.
Native registered operator login remains available independently of CRM service bindings.
CRM session creation and integration APIs fail closed until an operator approves exact
scope assignments for the hashed integration credential. No scope is auto-granted.

## Server and preserved configuration

VPS `/opt/camera-eye`, project `camera-eye`. Both existing files MUST be used:

* `/opt/camera-eye/docker-compose.cloud.yml`
* `/tmp/camera-eye-attendance-recovery.yml`

Do not use the installed `/usr/local/sbin/camera-eye-ci-deploy`: it replaces Compose.
Do not dispatch its GitHub workflow. Preserve `.env`, encryption key, camera configuration,
PostgreSQL/evidence/model/update volumes and historical records. Do not rebuild Windows.

Previous running tag: `camera-eye-portal:attendance-recovery-a48f162`.
Previous immutable image ID:
`sha256:db5ed30d285c7462422d032e4a9ac574ba219a22a62e8a617c3f7fb1bbd1a9b9`.

Verified backup: `/opt/camera-eye/backups/final-release-a531-gsjv454n`.
Includes both Compose files, `.env`, previous image archive, database dump, exact baseline
source, checksums, and verification JSON. Baseline dump restore and release bootstrap
passed in network-isolated PostgreSQL 17; protected table counts unchanged. Original
backup must never be overwritten. Capture a new backup before any later production write.

## Minimal fixes and provisioning

New preparation directory: `/opt/camera-eye/backups/mvp-prep-y2tjp6wb`.
Reviewed override: `/opt/camera-eye/backups/mvp-prep-y2tjp6wb/release-image.yml`.
Candidate PostgreSQL migration/binding/revocation checks passed against a restored,
network-isolated copy; historical status counts unchanged. Original backup checksums
and rollback image availability reverified. This preparation is NOT a deployment.

`crm_integration_scopes` is additive, initially empty. Primary key is
`(token_hash,tenant_id,shop_id)`; enabled flag, approving operator and timestamp are retained.
The hash uses the existing SHA256 convention. Environment allow-lists are not authority.
Grant only exact, operator-approved tenant/shop pairs. Edge credentials and personnel
directory membership do not authorize a different integration credential.

After approval, an operator can provision a scope using `PostgresPortalStore` and
`set_crm_integration_scope(sha256(existing_key), tenant, shop, granted_by=operator)`.
Supply approved values interactively; never echo the key or place it in shell history.
Revocation uses `enabled=False`. Rotation requires explicit bindings for the new digest.
No public binding/provisioning endpoint is introduced. Unlimited exact grants are supported.
The subsequent onboarding fix binds the configured CRM key automatically when an
authenticated OWNER/ADMIN/SUPERADMIN generates a code for their own shop, or when
a valid stored one-time code is consumed. The consumed code's stored scope is used;
company/shop values submitted by the installer cannot expand it. Invalid/global
development codes do not auto-grant scopes. Existing device credentials remain intact.
Legacy license requests without shop_id require an existing enabled site/edge mapping
whose shop is bound to that integration credential; new sites supply an approved shop_id.

Release quarantine is the exact nine historical event IDs in the operator-reviewed
release override. Dispatch returns a reconciliation-required acknowledgement before
CRM authentication or receipt writes. Malformed quarantine configuration fails closed.
No worker startup performs a historical receipt sweep. Edge redelivery remains guarded.
Do not remove quarantine IDs until external CRM reconciliation is approved.

## Manual attendance contracts

Directory: GET `/api/User/face-embeddings/{tenantCode}`, no static-token authentication.
Face Login: POST `/api/Auth/loginUsingFaceTenant`, CRM-enrolled base64Image + CRM tenant UUID.
Validate employee/tenant response identity; encrypted employee-specific token cache retains
expiry handling. Never log images, embeddings or tokens.

Login/Logout: POST `/api/UserRoster/LoginLogout`; raw employee token in Authorization.
Login body: userId, date with effective shop offset, actualStartTime as HH:mm:ss.
Logout body: userId, same date format, actualOffTime as HH:mm:ss. HTTP 2xx AND boolean
`success:true` are required. Login's observed message is `Logged in successfully.`;
live Manual Logout success remains unverified. Preserve safe messages. Ambiguous/timeout
mutations require reconciliation, not blind retry; cloud owns bridge CRM delivery.

Break local functionality remains; CRM break contracts deferred. No live break testing.
Auto Logout `/api/UserActivity/auto-logout` response excerpt `{"success"}` is invalid JSON;
full response and employee-token Bearer authorization are unverified. Keep disabled.
No absence-driven bridge checkout in this release.

## Historical state and flag gate

Eight RETRY receipts (all attempts=0) and one RECONCILIATION_REQUIRED receipt retained.
Root automatic entry has a recognition predecessor without a receipt; break-start was
quarantined as HISTORICAL_RECEIPT_MISSING. Remaining events form downstream chains.
No historical success can be inferred from missing receipts or zero cloud attempts.

Server base Compose defaults automatic Login to 1; original live setting remains 1 until
deployment approval. New release override explicitly sets:

```
SNAPKEY_CRM_AUTO_LOGIN_ENABLED=0
SNAPKEY_CRM_AUTO_LOGOUT_ENABLED=0
CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false
```

## Image-only deployment — approval required

Do not deploy baseline a531 unchanged. Build only the new committed release archive.
Use LF-preserving export: `git -c core.autocrlf=false archive --format=tar.gz --output=release.tar.gz RELEASE_SHA`.
Verify source archive checksum on the VPS and critical files against the commit before build.
Build `Dockerfile.cloud` with `org.opencontainers.image.revision=RELEASE_SHA` and tag
`camera-eye-portal:RELEASE_SHA`. Record `docker image inspect -f '{{.Id}}' TAG` and pin that
immutable image ID. A local image ID is not an OCI registry manifest digest.

Use the prepared release override (quarantine IDs, all automatic flags off) in addition
to both server Compose files; never substitute it for the recovery override.

```bash
set -euo pipefail
B=/opt/camera-eye/backups/final-release-a531-gsjv454n
(cd "$B" && sha256sum --check SHA256SUMS)
docker image inspect "$(cat "$B/previous-image")" >/dev/null
compose=(docker compose --project-name camera-eye --env-file /opt/camera-eye/.env
  -f /opt/camera-eye/docker-compose.cloud.yml
  -f /tmp/camera-eye-attendance-recovery.yml)
# Set immutable image ID and the exact reviewed new release override path first.
: "${CAMERA_EYE_RELEASE_IMAGE:?required}"
: "${RELEASE_OVERRIDE:?required}"
export CAMERA_EYE_RELEASE_IMAGE
"${compose[@]}" -f "$RELEASE_OVERRIDE" config --quiet
# STOP: explicit approval required for all commands below.
"${compose[@]}" -f "$RELEASE_OVERRIDE" run --rm --no-deps -T portal python -c \
 'from cloud_portal.postgres_storage import PostgresPortalStore; s=PostgresPortalStore(); s.engine.dispose()'
# Explicitly approved integration bindings must be provisioned before CRM-backend acceptance.
"${compose[@]}" -f "$RELEASE_OVERRIDE" up -d --no-deps --no-build --pull never portal
curl -fsS --retry 12 --retry-delay 5 --retry-connrefused https://camera.snapkey.ai/health
curl -fsS https://camera.snapkey.ai/portal/attendance.html | grep -q attendance-workspace
curl -fsS https://camera.snapkey.ai/static/js/main.js | grep -q AttendanceWorkspace.mount
```

## Post-deploy read-only acceptance and support

Assert the portal `.Image` equals the approved immutable ID, and inspect ONLY the three
automatic flag values (never dump Docker environment). PostgreSQL must remain healthy.
Unauthenticated workspace API must return 401/403. Signed-in operators must see only
their own tenant/shop history, policy, CRM messages and scoped images/videos.
Cross-tenant/shop requests must be denied. Desktop/mobile baseline acceptance already
passed; targeted UI smoke results for this release are reported separately.
Use synthetic identities for mutations; live Manual Login/Logout needs separate approval.
Inspect pending historical receipt counts without changing them.

```bash
# Local administrator inspection only: logs can contain customer identifiers.
docker logs --tail 80 camera-eye-portal-1
curl -fsS --max-time 10 https://camera.snapkey.ai/health
docker inspect -f '{{.State.Health.Status}}' camera-eye-postgres-1
docker exec -e 'PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=5000' \
 camera-eye-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c \
 "SELECT status,count(*) FROM edge_attendance_delivery GROUP BY status ORDER BY status"'
```

## Image-only rollback — retains disabled flags and quarantine

```bash
previous=$(cat "$B/previous-image")
docker image inspect "$previous" >/dev/null || docker image load -i "$B/previous-image.tar"
export CAMERA_EYE_RELEASE_IMAGE="$previous"
"${compose[@]}" -f "$RELEASE_OVERRIDE" up -d --no-deps --no-build --pull never portal
curl -fsS --retry 12 --retry-delay 5 --retry-connrefused https://camera.snapkey.ai/health
```

No down, volume deletion or automatic database restore. Keep additive migrations/history.
Rollback command semantics were previously harness-verified; production rollback is not executed.
The older image does not implement the new quarantine guard: pending chains stay unreconciled,
automatic flags stay off, and historical replay remains prohibited operationally.
