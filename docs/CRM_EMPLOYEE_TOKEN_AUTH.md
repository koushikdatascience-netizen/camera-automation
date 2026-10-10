# Employee attendance authentication

Camera Eye uses the confirmed CRM Face Login token for an employee's check-in,
checkout, Break In, Break Out, and qualifying absence logout. Static service JWTs
are not used by these employee mutation paths.

| CRM operation | Endpoint | Authentication / unchanged payload |
| --- | --- | --- |
| Tenant identity directory | `GET /api/User/face-embeddings/{tenantCode}` | Public; resolve `id`, `tenantId`, and enrolled `faceImages`/`profileImage` from the requested tenant only |
| Obtain employee token | `POST /api/Auth/loginUsingFaceTenant` | Public; `base64Image`, `tenantId`; verify returned `user.id` and any returned `user.tenantId` |
| Check-in / normal checkout | `POST /api/UserRoster/LoginLogout` | Employee token; existing `userId`, `date`, `actualStartTime` or `actualOffTime` |
| Break In | `POST /api/UserBreak/start-break` | Same employee token; `userId`, `breakMasterId` |
| Break Out | `POST /api/UserBreak/end-break` | Same employee token; `userId` query parameter |
| Qualifying absence logout | `POST /api/UserActivity/auto-logout` | Same employee token; `userId`, `remarks`; retain the confirmed 60-minute minimum and person-policy checks |

The returned token is forwarded in the Authorization header using the existing
CRM header contract. No refresh endpoint or alternative employee authentication
flow is introduced. The same token is reused while valid.

## Vault and failure behavior

The existing PostgreSQL `crm_face_tokens` table stores Fernet-encrypted tokens
keyed by tenant code, shop, and CRM user ID. Scope metadata inside the ciphertext
prevents copying a token to a different tenant/user row. Each lookup also verifies
current membership in that tenant's face directory. JWT `exp`, when present,
is only an unverified expiry hint with a five-minute safety margin. Opaque tokens
are not cached by default because the CRM token lifetime is not verified; an
operator may configure `SNAPKEY_CRM_TOKEN_FALLBACK_TTL_SECONDS` only after CRM
confirms a safe lifetime. No 24-hour duration is assumed.

Manual actions still require a fresh recognition candidate from the correct
camera and edge. On a cache miss, they authenticate the current recognition image;
automatic operations use the existing CRM-enrolled Base64 image. When an expired
token cannot be renewed, the action fails without service-token fallback. Never
use another employee's token.

An HTTP 401 invalidates only the rejected employee's cached token. There is no
immediate replay of the attendance mutation. V2 absence timeouts and ambiguous
results retain the existing reconciliation requirement. CRM business rejection
must not produce a successful local manual-attendance update. Evidence manifests,
attendance rules, and independent automatic-logout flags remain in place.

## Administrative reads kept separate

`GET /portal/v1/tenants/{tenant_id}/crm/users` now uses the public tenant directory
and returns identity fields only, without images or embeddings.

Monthly `GET /api/UserRoster/GetUsersRoster` keeps its tenant-scoped service
credential and directory/roster isolation checks. Authorization of an employee
Face Login token for a whole monthly roster is not established by the confirmed
employee mutation contract.

The tenant-wide Camera Eye `GET /portal/v1/tenants/{tenant_id}/crm/breaks` request
contains no employee identity. Its administrative `GET /api/BreakMaster/my-breaks`
lookup remains separate and scope-checked. Migrating this route to an employee
token requires a selected employee identifier in the Camera Eye API/caller; it
must never silently choose an employee from the directory. This does not prevent
mapped employees from starting/ending breaks with their own tokens.

## Deployment prerequisites (not executed by this change)

- Keep PostgreSQL configured. The SQLite cloud fallback has no token vault and
  fails closed for employee CRM authentication.
- Set `CAMERA_EYE_TOKEN_ENCRYPTION_KEY` to a stable Fernet key in server secrets
  before using any employee attendance action. It now applies to all employee
  actions, not only V2 absence handling. Retain the existing key to preserve caches.
- No database schema migration is added; existing scoped cache entries remain
  usable until expiry or invalidation.
- Service-token settings are optional for employee actions; retain them only
  for administrative roster/break-catalogue reads that still need them.
- Keep both automatic-logout flags disabled until separately approved staging
  verification. No production service, configuration, data, or installer is changed.
