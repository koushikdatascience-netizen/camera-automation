# Camera Eye final CRM release gate

Status: implementation and mock-contract preparation only. No production CRM
mutation, deployment, workflow dispatch, Windows installer build, or production
database change was performed for this release gate.

## Endpoint and identity matrix

| Purpose | Method and path | Auth and identity | Contract evidence / release status |
| --- | --- | --- | --- |
| Employee directory and enrollment image | `GET /api/User/face-embeddings/{tenantCode}` | No Authorization header. Directory is scoped by tenant code; map CRM `id` and `tenantId`. | Customer-supplied integration notes and implementation. Real response shape must be confirmed for each tenant; no images or embeddings are returned by ordinary personnel-list APIs. |
| Employee Face Login | `POST /api/Auth/loginUsingFaceTenant` | No Authorization header. `{base64Image, tenantId}` uses the CRM-enrolled image and CRM tenant UUID. Returned user and tenant are checked before caching. | Customer-supplied contract and mocked response. Real token/identity response not exercised in this task. |
| Check-in and ordinary checkout | `POST /api/UserRoster/LoginLogout` | Employee Face Login token. `userId` is the mapped CRM employee ID; `date` is ISO date; `actualStartTime` or `actualOffTime` carries the action time. | Path, fields and token behavior are documented and mock-tested. Exact live success and rejection schemas remain unverified. |
| Break In | `POST /api/UserBreak/start-break` | Same employee token; `{userId, breakMasterId}`. Break ID comes from the tenant/shop-scoped CRM mapping. | Path/body are documented and mock-tested. Exact live business response remains unverified. |
| Break Out | `POST /api/UserBreak/end-break` | Same employee token; `userId` query parameter. | Path/query are documented and mock-tested. Exact live business response remains unverified. |
| Absence logout | `POST /api/UserActivity/auto-logout` | Same employee token; `{userId, remarks}`. Requires policy eligibility, healthy scoped camera coverage, and the confirmed 60-minute minimum. | Customer-supplied contract and mock-tested. Keep both automatic logout flags off until controlled staging and explicit approval. |
| Monthly roster | `GET /api/UserRoster/GetUsersRoster` | Tenant-scoped service token; `year`, `month`, and `userId`. Kept separate from employee attendance tokens. Returned rows are further restricted to enabled CRM-user mappings in the caller's shop. | Existing path and code. Service-token tenant scope mismatch can fail closed; CRM must provision the appropriate tenant-scoped credential. Live credential and response scope were not verified. |
| Break catalogue | `GET /api/BreakMaster/my-breaks` | Existing administrative service-token path. | No safe employee identity is selected by the current tenant-wide portal operation; not migrated to an employee token. |

Camera Eye policy endpoints (`/integration/v1|v2/.../attendance-policy` and
`/portal/v2/.../attendance/policy`) and evidence/media endpoints are internal
Camera Eye APIs. There is no verified CRM policy-sync or CRM evidence-upload
endpoint in the supplied contract, so none is invented here. CRM employee mapping
preserves tenant, shop, local-person, CRM-user, and break-master identifiers.

## Mutation confirmation and recovery

The CRM client now accepts a mutation as confirmed only when the HTTP request is
successful and its JSON object explicitly has `success: true` or `ok: true`.
An explicit JSON `success: false` or `ok: false` is treated as a business
rejection. Empty, non-JSON, or other 2xx bodies raise `CrmUnconfirmedMutationResponse`;
the attendance bridge records these as `RECONCILIATION_REQUIRED` because the
remote action may have succeeded. It does not automatically resubmit. The
non-JSON body is not copied into logs or return values. This is a fail-closed
client-side rule, not proof that the live CRM uses these success fields. Confirm
those fields and business rejection semantics with the CRM team before enabling
mutations. Existing confirmed-rejection/retry classification must be revisited
if the CRM returns a different contract.

For a timeout, malformed response, or unrecognized 2xx response, an operator
must compare the employee's CRM roster/activity state for the exact shop and
event time before using the scoped reconciliation action. Never replay a
historical event merely because its receipt is absent. Preserve evidence and
delivery rows during review.

## Deployment and rollback gate

The cloud workflow is `workflow_dispatch` only and binds the deploy job to the
GitHub `production` environment. Repository administrators must configure
required reviewers and deployment branch restrictions under **Settings →
Environments → production**; YAML cannot configure that repository-side
protection. Do not dispatch before protection, CRM credentials, encryption key,
database backup/restore validation, and staging checks are complete. No workflow
dispatch was requested or performed here.

Before deployment, follow
[`CAMERA_EYE_V2_STAGING_AND_ROLLBACK.md`](CAMERA_EYE_V2_STAGING_AND_ROLLBACK.md)
for the isolated PostgreSQL staging run, encrypted backup, pre-deploy checks,
and rollback to the recorded prior image/revision. Keep automatic CRM logout
disabled. The Windows installer was not rebuilt; this change is cloud-only.

## Remaining release blockers

- The CRM team's live success/rejection response bodies and status semantics
  have not been verified against a designated test employee.
- Monthly roster access still requires a correctly scoped service credential;
  employee Face Login authorization for that endpoint is unverified.
- No live staging CRM, SMTP, WhatsApp, production environment protection, or
  deployment verification was performed as part of this local change.
- Evidence and attendance policy remain Camera Eye-managed; external CRM
  delivery for either is unsupported by a verified endpoint contract.

## Local verification on 2026-10-10

- Isolated PostgreSQL attendance/policy/CRM delivery/concurrency/tenant suite:
  `176 passed, 1 skipped` on a disposable local PostgreSQL cluster. The skipped
  test requires a separate optional test service. The isolated cluster was
  stopped and removed after the run; no production database was used.
- PostgreSQL upgrade regression for the legacy edge-credential identity index:
  `1 passed`; the disabled credential remained intact while a new active
  credential was provisioned.
- Latest roster shop-scope, tenant-scope, cloud portal, and workflow tests:
  `22 passed` in 49.83s. The roster endpoint now only returns enabled mappings
  belonging to the authenticated principal's shop, and rejects an explicit
  user outside that shop.
- Full local suite on the pre-shop-scope/workflow-default snapshot:
  `395 passed, 19 skipped`. This was not rerun after the small changes above;
  the affected suites and PostgreSQL upgrade regression were rerun instead.
- Docker Desktop's Linux engine was unavailable. Isolated PostgreSQL was run
  using the installed local PostgreSQL service on a separate loopback port and
  disposable test database/schema.
- The Windows edge-update workflow's `publish` input now defaults to false;
  static YAML tests verify manual dispatch, production environment binding, and
  no push trigger. Actual GitHub environment reviewers/branch restrictions are
  not observable because GitHub API access failed through the local proxy.
- CRM success/rejection/401 response bodies and monthly-roster credential scope
  remain MOCK-VERIFIED only. No CRM mutation, staging release, workflow dispatch,
  browser run, or installer build was performed. Exact CRM `date` business
  timezone contract also remains unverified.
- `tools/deploy_cloud.sh` rollback code was inspected: it restores the prior
  image and Compose file after a failed health check. The script could not be
  executed locally because Bash/WSL startup is denied. The schema upgrade uses
  idempotent `CREATE/ALTER ... IF NOT EXISTS`, but replaces the legacy
  `idx_edge_credentials_identity` index with a partial active-identity index;
  schema downgrade is not automatic. Back up before deploy and retain the
  previous image/Compose revision for application rollback.
