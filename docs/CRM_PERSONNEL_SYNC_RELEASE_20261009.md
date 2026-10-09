# CRM personnel synchronization release — 2026-10-09

Base: `d4f62176bbc7e6237207918679547077ee78a9e2` on
`codex/crm-enrollment-cache-20261009`. Changes are isolated on
`codex/crm-personnel-previews-20261009`; use that branch's final local HEAD as the
release revision. This report supplements the earlier enrollment hardening report.

## Changes and causes

- Employee-code-first resolution could create a second identity when CRM changed
  an employee code. Resolve the existing CRM user mapping first; reject duplicate
  directory users, identity reassignment and employee-code collisions. Do not
  merge ambiguous local identities or rewrite their attendance history.
- Refresh omitted `break_master_id` but mapping upserts cleared its previous value.
  Preserve the approved break mapping unless the caller explicitly supplies it.
- A model fingerprint calculated during export cannot prove the model used to
  create a previously cached vector. Persist the generating fingerprint with each
  template. Backfill only after verifying the existing image/model-bound cache key
  and valid vector. Unverified CRM templates are withheld until refresh validates
  them. Mixed model generations block export. Edges check the fingerprint and
  512-dimensional finite, nonzero embeddings before applying the atomic snapshot;
  incompatible stored templates are also excluded from recognition after a model change.
- Enrollment previews were missing for cloud-mirrored people. Fetch image bytes
  on demand through authenticated, tenant/shop-scoped proxy routes. No image,
  Base64 value or embedding is added to ordinary personnel-list responses.
- Face Login and cached-token membership checks reject duplicate identities,
  mixed tenant directories, explicit tenant/shop disagreement and inactive users.
  Face Login still uses the employee's CRM-enrolled image, validates the response's
  employee/tenant, and preserves the existing encrypted, expiring user-specific vault.
- Image processing supports CRM string/list `faceImages` and `profileImage` fields,
  raw Base64 and image data URLs, including JPEG `/9j/`. Arbitrary remote URLs or
  filesystem paths are rejected; no unverified server-side URL fetch is introduced.

## Routes and UI

| Route | Authorization / behavior |
|---|---|
| `GET /portal/v1/tenants/{tenant}/personnel/{person}/enrollment-image` | Portal session; exact tenant and session shop; mapped active employee only |
| `GET /edge/v1/personnel/{person}/enrollment-image` | Scoped edge credential; legacy global credentials rejected |
| `GET /api/v1/personnel/{person}/enrollment-image` | Loopback host/origin guard, HttpOnly SameSite=Strict operator-preview capability issued by `/setup`, exact configured tenant/shop; cloud edge token stays server-side |
| `GET /portal/v1/tenants/{tenant}/personnel-diagnostics` | Session-scoped read-only mirror and heartbeat inspection; no CRM refresh or database writes |
| Existing local personnel list | Adds CRM mapping, local template count and model compatibility diagnostic; removes internal serialized synchronization metadata |
| Existing authenticated edge personnel configuration | Adds CRM user, tenant/shop and generating model fingerprint; templates remain restricted to the edge configuration API |

Cloud previews load with the existing authenticated fetch helper into temporary
blob URLs, revoked after loading. Preview responses have `no-store`, `nosniff`
and same-origin resource policy. Local proxy requests do not follow redirects,
have the configured timeout and an 8 MiB response cap. Missing images or upstream
failures produce explicit unavailable responses; they never generate synthetic previews.

Diagnostics expose template counts, mapping, model fingerprints and per-edge
template-ID comparisons with heartbeat timestamps. Heartbeats are historical
reports, not live recognition proof. Missing model information is `UNVERIFIED`.

## Additive migrations / preserved behavior

- Cloud SQLite and PostgreSQL: nullable `cloud_face_profiles.model_key`.
- Edge SQLite: nullable `personnel.crm_sync_json` containing identity scope and
  fingerprint only, without images, embeddings or credentials.
- PostgreSQL mapping updates take a transaction-scoped advisory lock; its existing
  tenant/shop/CRM-user unique index remains in force. SQLite performs collision
  checks within its mapping transaction.
- No attendance/history reset or historical delivery replay. Manual/automatic
  attendance, policies, evidence completion, offline queues, idempotency and
  automatic-logout flags are unchanged. The additional columns are backward
  compatible and need not be dropped for an application rollback.

## Verification

The final recorded counts are appended below after the test run. PostgreSQL tests
use only `camera_eye_test` on loopback port 55432 in the separate
`camera-eye-personnel-tests` Docker project. Each fixture creates and removes only
its own uniquely named test schema. Existing application containers are untouched.

The PostgreSQL enrollment regression uses a synthetic JPEG, mocked single-face
enrollment and mocked CRM directory/authentication results. It exercises actual
PostgreSQL persistence, authenticated edge configuration, SQLite mirroring,
recognition matching and preview generation. This is not live CRM or webcam proof.

Reproduce against the already provisioned isolated test database in PowerShell:

```powershell
$env:SNAPKEY_TEST_DATABASE_URL = 'postgresql+psycopg://camera_eye_test:<local-test-password>@127.0.0.1:55432/camera_eye_test'
$env:SNAPKEY_REQUIRE_POSTGRES_TESTS = '1'
$testTemp = Join-Path $env:TEMP ('camera-eye-personnel-test-' + [guid]::NewGuid().ToString('N'))
python -m pytest -q tests --basetemp=$testTemp --junitxml=artifacts/personnel-suite.xml
python -m compileall -q cloud_portal camera_service
node --check cloud_portal/static/js/main.js
git diff --check
```

Replace the password placeholder with the existing isolated test-service credential.
Do not substitute a production database URL. No biometric test artifacts
or credential files belong in the release commit.

## Release gates and exact sequence — commands for review only

1. Record the immutable local release revision:
   `git rev-parse codex/crm-personnel-previews-20261009`.
   After explicit approval only, push this branch:
   `git push -u origin codex/crm-personnel-previews-20261009`.
   Do not dispatch a production workflow or enable either automatic-logout flag.
2. Build the cloud staging image from that immutable checkout in PowerShell:
   `$releaseSha = git rev-parse HEAD`, then
   `docker build -f Dockerfile.cloud -t "camera-eye-personnel:$releaseSha" .`.
   Use an isolated staging PostgreSQL database, staging edge credential and
   identical cloud/edge buffalo_l model weights. Keep production Compose and `.env`
   files server-owned. Inspect the diagnostic before recognition testing.
3. With CRM-team approval and a dedicated test employee, verify directory scope,
   enrollment preview, local recognition and Face Login identity/token expiry.
   Verify the four attendance/break actions only in their authorized CRM test
   environment; this task does not perform those external mutations.
4. Only after acceptance and separate deployment approval, back up the real
   database using the established server backup procedure, record its current
   immutable portal image, and update the portal image through the existing
   approved image override. Do not copy repository Compose onto the server.
   The exact command shape is:
   `docker compose --env-file "$SERVER_ENV" -f "$SERVER_COMPOSE" -f "$APPROVED_IMAGE_OVERRIDE" up -d --no-deps portal`.
   Those paths must come from the actual server deployment configuration; this
   task does not inspect or guess them. Verify health, scoped previews and diagnostics.
5. An edge update is required to deliver the new local preview and fingerprint
   checks. Prepare a Windows installer only after separate approval and cloud
   staging acceptance; none is built in this task.

Rollback selects the recorded previous portal image in the same approved override
and repeats the command in step 4. Restore the previous approved edge binary if
necessary. Leave the additive columns and all attendance/evidence records intact.
Do not blindly restore databases or replay events: an application rollback cannot
undo CRM actions. Older software lacks the new fingerprint enforcement, so test
rollback recognition compatibility before accepting the old version.

## Remaining external verification and risks

- Live CRM responses, real enrolled images, token permissions and physical-camera
  matching are not verified by mocks. Browser visual QA of these new UI additions
  is not performed in this task; JavaScript syntax and HTTP authorization are tested.
- The confirmed upstream face directory is unauthenticated. Camera Eye's proxy
  protections do not secure the CRM team's publicly accessible source endpoint.
- Local preview authentication follows the existing trusted-loopback workstation
  model, adding a browser capability. It is not an independent per-Windows-user
  login or multi-user authorization system.
- Existing duplicate mappings or employee-code collisions require explicit
  operator reconciliation; this release rejects them without altering history.
- A model mismatch intentionally blocks snapshot application and recognition of
  incompatible cached templates. Align the actual model files and refresh before
  client acceptance. Missing/invalid images remain unenrolled, never fabricated.

## Final recorded results

- Complete suite on the final code: **363 passed, 0 failed, 0 skipped**, 114.22 s.
  Includes **9 actual isolated PostgreSQL tests** on `postgres:16-alpine`.
  Result: `artifacts/personnel-release-final-suite.xml`.
- Focused enrollment/preview/SQLite/PostgreSQL/token tests: **67 passed**, 65.38 s.
  Result: `artifacts/personnel-final-focused.xml`.
- Corrected employee authentication/attendance mocked contracts: **20 passed**,
  6.12 s. Result: `artifacts/personnel-employee-token-contract-final.xml`.
- Initial full run found 17 failures: 16 legacy authentication fixtures used a
  non-Base64 placeholder; one newly added PostgreSQL mock omitted the real
  business-success predicate. Those fixtures were corrected, cross-tenant
  rejection retained its specific missing-user error, and the final full rerun passed.
- `compileall`, cloud JavaScript syntax, extracted local setup JavaScript syntax
  and `git diff --check` passed. Existing deprecation warnings remain.
- No live CRM/physical-camera/browser visual acceptance is claimed. No VPS
  deployment, push, production data changes or Windows installer build occurred.
