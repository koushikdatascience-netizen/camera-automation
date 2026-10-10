# Controlled Camera Eye release — 2026-10-10

Status: **BLOCKED for production automatic-logout activation; controlled cloud upgrade package prepared for approval.** The exact release SHA is the local commit reported in the handoff. Nothing was pushed, deployed or rebuilt.

## Fast audit and existing functionality

Working branch: `codex/crm-policy-alerts-finalization-20261010`, based on `398e2404e5345eb09b95c368803008c0a41d0298`.
Read-only VPS inspection: checkout `3bdf77c2dc29c5994276888584dff1cf45a41c52`; `/health` returns status=ok, service=snapkey-portal. Served attendance HTML/JS have no AttendanceWorkspace marker; the V2 workspace route returns 404. This is a deployment/version gap, not a reason to rebuild the UI. Docker image inspection and the deployment revision file were denied to the restricted SSH account; the running image SHA is **unverified**.

Source already has `/portal/attendance.html`, Attendance Policy navigation at `#policy`, `/portal/v2/tenants/{tenant_id}/attendance/workspace`, personnel filters/history, authenticated evidence previews, policy APIs and alerts/delivery status. Cloud manual actions use `/portal/v2/tenants/{tenant_id}/attendance-station/action`. Existing session/tenant/shop guards and durable delivery receipts are reused.

Edge commits local attendance plus queue events atomically. Cloud is the CRM dispatch owner for bridge events: it resolves tenant/shop/person mapping, claims the immutable event/session identity, dispatches and stores confirmation before local finalization. Recognition is not an attendance predecessor. Duplicate ingestion does not repeat CRM. Cloud manual actions use the same bridge with stable `portal-manual:` identities and per-person serialization. Unknown remote results remain reconciliation-required.

**Important product boundary:** bridge sessions still require explicit checkout. V2 policy absence logout applies to non-bridge presence sessions; maximum-logoff retains its existing scope. Do not advertise disappearance-driven logout for bridge sessions. Changing this would require approved cloud/edge state reconciliation, not merely enabling a flag. Break contract changes are deferred.

## Verified fixes and migrations

- Manual Logout: raw employee token; exact userId/date/actualOffTime body; shop-offset ISO date and HH:mm:ss time. Auto Login is unchanged.
- Auto Logout: separate auto-logout endpoint, Bearer token, effective policy threshold with the existing 60-minute safety floor. Unknown success bodies never confirm delivery. Employee endpoint scope remains unverified.
- Legacy absence no longer bypasses V2 claims/reconciliation through LoginLogout. Maximum-logoff now uses durable claim/confirmation/finalization and preserves its on-break rule.
- PostgreSQL additive `crm_auto_logout_actions.confirmation_metadata JSONB NOT NULL DEFAULT '{}'`; preserves safe CRM message, original action time and policy snapshot. Recovery does not resubmit CRM. Conflicting unresolved logout claims block newer claims.
- No new SQLite migration in this release. Existing additive edge queue/attendance migrations remain unchanged.
- Local deploy handler preserves server Compose and .env. The currently installed VPS handler still overwrites Compose: **do not use it for this release**. The manual image-only procedure below avoids replacing that handler.
- Compose now passes the fail-closed integration scope allow-list. Existing server Compose must receive the equivalent approved configuration via a separate override; never overwrite it.

## Test evidence

- Previously requested full run: **437 passed, 4 failed, 4 skipped**, 896.36 seconds. It started before final corrections. The four failures were reconciliation variable reference, obsolete logout-body assertion, token test assuming identical raw headers across endpoints, and an opaque-token fixture lacking explicit expiry/TTL.
- All four failing nodes passed targeted reruns against corrected code. Final API/contract/PostgreSQL/workflow gate: **25 passed**; browser subprocess launch was sandbox-blocked, then both browser tests passed outside the sandbox (**2 passed**).
- Employee token regression suite: **21 passed**. Final max-logoff focused checks: **3 passed**. New PostgreSQL conflicting-claim/on-break regression: **1 passed**. Earlier PostgreSQL group: 28 passed, 1 failed, 1 skipped; its failed fixture was corrected and passed the final targeted gate.
- Bash deployment harness: PASS for restricted command, revision mismatch rejection, successful image switch, rollback, preserved server Compose and no destructive Docker commands.
- Compileall and git diff checks passed. No repeated full suite was run after these corrections.
- Real Chrome, local synthetic SQLite and cloud isolated PostgreSQL 14: filtering, employee policy, four-action history, CSV and mobile layout passed; local three-image/short WebM preview played. Screenshots/exports: `artifacts/release-gate-20261010-logout/`. CRM mutations were mocked. No new physical-camera or live CRM verification is claimed. Cloud scoped-media resolution is covered by API regressions; real edge uploads/provider delivery require controlled acceptance.

## Exact changed files

Runtime/configuration: `.env.cloud.example`, `cloud_portal/api.py`, `cloud_portal/crm_client.py`, `cloud_portal/postgres_storage.py`, `docker-compose.cloud.yml`.
Deployment: `tools/deploy_cloud.sh`, `tools/test_deploy.sh`.
Tests: `tests/attendance_browser_support.py`, `tests/test_attendance_defect_fixes.py`, `tests/test_cloud_portal.py`, `tests/test_crm_employee_tokens.py`, `tests/test_crm_face_attendance.py`, `tests/test_crm_logout_contract.py`, `tests/test_deployment_workflow_safety.py`, `tests/test_postgres_attendance_bridge.py`, `tests/test_postgres_attendance_integration.py`.
Documentation: this file, `docs/CRM_LOGOUT_RELEASE_GATE_20261010.md`, `docs/ATTENDANCE_SYNC_BRIDGE.md`, `docs/CAMERA_EYE_V2_STAGING_AND_ROLLBACK.md`.

Compose was validated using synthetic required values without a Docker daemon; both documented Bash command blocks passed syntax checks. No Docker image was built.

## Environment checklist

Provision these using the server secret manager, without printing values:
- Existing POSTGRES_DB/USER/PASSWORD, SNAPKEY_DATABASE_URL, activation/license/edge credentials, and evidence/update/model volumes.
- CAMERA_EYE_TOKEN_ENCRYPTION_KEY: backed-up Fernet key. Never regenerate an existing key without token-vault rotation.
- SNAPKEY_CRM_INTEGRATION_KEY plus nonempty, approved SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES JSON list of tenant/shop grants. Empty list denies integration.
- SNAPKEY_CRM_API_BASE_URL=https://apis.snapkey.in and SNAPKEY_CRM_FACE_LOGIN_URL=https://apis.snapkey.in/api/Auth/loginUsingFaceTenant.
- Employee/shop policy timezone; optional SNAPKEY_CRM_ATTENDANCE_TIMEZONE override. Opaque token fallback TTL stays 0 unless CRM expiry evidence permits a configured TTL.
- Keep SNAPKEY_CRM_AUTO_LOGIN_ENABLED=0, SNAPKEY_CRM_AUTO_LOGOUT_ENABLED=0 and CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false until explicit acceptance/activation approval.
- Administrative monthly-roster credentials are separate and optional for employee attendance. SMTP/WhatsApp provider credentials and delivery approval are required for real notification acceptance; do not send test messages to real recipients.

## Deployment commands — REVIEW ONLY, do not execute without approval

The release is local-only. On the current Windows checkout, export exactly the reviewed commit (no untracked files or secrets):

```powershell
$release = git rev-parse HEAD
git archive --format=tar.gz --output="camera-eye-source-$release.tar.gz" $release
# After transfer approval, copy that archive to the authorized release host.
# Do not push, trigger workflows or transfer automatically.
```

On the approved Docker host, operating as the authorized deployment administrator:

```bash
set -euo pipefail
umask 077
release=REPLACE_WITH_EXACT_HANDOFF_SHA
project=/opt/camera-eye
source_dir="$project/releases/$release"
mkdir -p "$source_dir"
tar -xzf "/approved-transfer/camera-eye-source-$release.tar.gz" -C "$source_dir"
docker build -f "$source_dir/Dockerfile.cloud" --label "org.opencontainers.image.revision=$release" -t "camera-eye-portal:$release" "$source_dir"
test "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "camera-eye-portal:$release")" = "$release"
cd "$project"
compose=(docker compose --project-name camera-eye --env-file "$project/.env" -f "$project/docker-compose.cloud.yml")
"${compose[@]}" config --quiet
portal=$("${compose[@]}" ps -q portal)
previous_image=$(docker inspect --format '{{.Image}}' "$portal")
backup_dir="$project/backups/release-$release"
mkdir -p "$backup_dir"
printf '%s\n' "$previous_image" > "$backup_dir/previous-image"
cp "$project/docker-compose.cloud.yml" "$backup_dir/server-compose.yml"
"${compose[@]}" exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_dir/database.dump"
test -s "$backup_dir/database.dump"
# Validate dump catalogue; restore into an isolated database before acceptance.
"${compose[@]}" exec -T postgres pg_restore --list < "$backup_dir/database.dump" > "$backup_dir/dump-catalogue.txt"
override="$backup_dir/release-image.yml"
cat > "$override" <<EOF
services:
  portal:
    image: camera-eye-portal:$release
    pull_policy: never
    environment:
      CAMERA_EYE_TOKEN_ENCRYPTION_KEY: \${CAMERA_EYE_TOKEN_ENCRYPTION_KEY:?configured backed-up Fernet key required}
      SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES: '\${SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES:?approved scope grants required}'
      SNAPKEY_CRM_API_BASE_URL: https://apis.snapkey.in
      SNAPKEY_CRM_FACE_LOGIN_URL: https://apis.snapkey.in/api/Auth/loginUsingFaceTenant
      SNAPKEY_CRM_AUTO_LOGIN_ENABLED: '0'
      SNAPKEY_CRM_AUTO_LOGOUT_ENABLED: '0'
      CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED: 'false'
EOF
"${compose[@]}" -f "$override" config --quiet
# Additive migrations only; this command is an approval-gated production write.
"${compose[@]}" -f "$override" run --rm --no-deps -T portal python -c 'from cloud_portal.postgres_storage import PostgresPortalStore; s=PostgresPortalStore(); s.engine.dispose()'
"${compose[@]}" -f "$override" up -d --no-deps --no-build --pull never portal
curl -fsS --max-time 10 http://127.0.0.1:8000/health
"${compose[@]}" -f "$override" ps portal
```

Preserve the exact existing project name if it differs from camera-eye; verify it before running commands. Retain the override path for future restarts. Never restart/pull PostgreSQL or delete volumes as part of this portal release. Building locally is currently blocked because Docker Desktop is not running; no image build is claimed here.

## Acceptance and rollback

After deployment approval, health must be OK; `/portal/attendance.html` must load the workspace and policy link. An unauthenticated workspace API request must return 401/403, not 404 or personnel data. Sign in with a scoped test operator; verify history, CRM messages, evidence and policy reads. Managers may read within their shop; administrators may change permitted policies. Deny other tenant/shop access.

CRM team must designate a staging employee, CRM UUID, tenant UUID/code and shop mapping; supply an enrolled image through the existing directory, not a chat attachment. Verify Face Login identity, raw-token LoginLogout Login/Logout bodies, boolean success=true and safe messages, rejection/expiry, duplicate recognition and timeout reconciliation. Auto Logout acceptance must first obtain the complete sanitized n8n response/status/content-type and prove the successful Bearer token was this employee's Face Login token with endpoint permission. No historical event may be replayed without external confirmation. Break changes are excluded.

If health/navigation fail, restore only the prior portal image:

```bash
previous_image=$(cat "$backup_dir/previous-image")
rollback_override="$backup_dir/rollback-image.yml"
printf 'services:\n  portal:\n    image: "%s"\n    pull_policy: never\n' "$previous_image" > "$rollback_override"
"${compose[@]}" -f "$override" -f "$rollback_override" up -d --no-deps --no-build --pull never portal
curl -fsS --max-time 10 http://127.0.0.1:8000/health
```

Keep additive columns/history. Never automatically restore over the production database. If confirmed corruption requires data rollback, stop portal writes only after approval, restore the dump into a separate database, validate it and explicitly approve switching to it. Preserve the original database and backup.

## Windows compatibility

No edge runtime changes or installer rebuild in this release. Cloud accepts the existing edge.event.v1 envelope and repairs recognition predecessors safely. Verify the installed edge emits stable event/session IDs, preserves its existing queue and receives scoped personnel/model configuration before acceptance. Existing historical missing receipts stay quarantined for reconciliation. The installed EXE version and current site camera flow were not reverified; do not claim a rebuilt installer is deployed. Preserve ProgramData, activation and SQLite history.

## Remaining release blockers

1. Exact Auto Logout success body and employee-token endpoint authorization are unverified. Flags remain off.
2. Bridge sessions intentionally do not check out solely on disappearance; product acceptance must respect this boundary or approve a separate state-reconciliation change.
3. Approve the cloud version upgrade and reviewed environment override. Do not dispatch the existing unsafe server handler; either use the manual image-only procedure or separately approve installing the corrected handler.

GitHub production required-reviewer protection was verified read-only; prevent_self_review is false. No workflow dispatch or settings change occurred.
