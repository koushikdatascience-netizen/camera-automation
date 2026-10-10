# CRM logout release gate — 2026-10-10

Auto Login from 398e240 is unchanged. All tests use synthetic identities and mocked CRM mutations.

| Action | Endpoint | Authorization | Body |
|---|---|---|---|
| Manual checkout | POST /api/UserRoster/LoginLogout | Raw employee Face Login token | userId, date (ISO 8601 with shop offset), actualOffTime (HH:mm:ss) |
| Absence logout | POST /api/UserActivity/auto-logout | Bearer employee Face Login token, pending staging scope verification | userId, remarks (effective policy absence threshold) |
| Maximum logoff | POST /api/UserRoster/LoginLogout | Raw employee Face Login token | userId, date, actualOffTime |

LoginLogout requires an HTTP success and JSON boolean success=true. Auto Logout uses the same conservative boolean confirmation rule provisionally; the supplied `{"success"}` excerpt is not valid JSON and does not establish the actual response contract. Other responses cannot confirm delivery. Explicit success=false retains the sanitized CRM rejection message; authentication errors invalidate the scoped cached token and uncertain results require reconciliation rather than replay.

V2 absence requires an open session, no break, enabled person policy, healthy relevant camera coverage and the existing 60-minute endpoint safety floor. The effective threshold is the larger of the person threshold and that floor; remarks record that actual threshold. Legacy grace processing cannot call normal checkout. Both automatic-logout flags remain disabled by default. Existing bridge sessions retain explicit-checkout semantics.

Maximum-logoff remains independent of camera coverage and may close an on-break session, preserving its existing rule. It now shares the durable action claim/finalization mechanism. Newer recognition during a CRM action preserves local state and raises reconciliation. Restart recovery finalizes confirmed actions locally without calling CRM again.

Database migration: additive `crm_auto_logout_actions.confirmation_metadata JSONB NOT NULL DEFAULT '{}'`. It preserves safe CRM messages, original action time and policy snapshot through local-finalization recovery. Existing rows default to an empty object; no historical action is replayed or assumed successful. Attendance evidence continues to use scoped recognition evidence; unavailable media remains explicitly unavailable.

Before release:
1. Obtain the exact Auto Logout HTTP status, content type and full sanitized response body from the existing n8n execution. Confirm that its Bearer credential is the same employee's Face Login token and scoped to the expected tenant/user. Do not infer this from a generic bearer example.
2. Verify manual checkout and absence logout using a designated staging employee with CRM approval; include expired tokens and rejection responses. No production mutations were used here.
3. Review the release, back up the cloud database, apply the additive migration on staging and keep SNAPKEY_CRM_AUTO_LOGOUT_ENABLED=0 and CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false until acceptance. Application rollback retains the added column and existing delivery records; reconcile uncertain actions externally before retry.

No deployment, push or installer rebuild is part of this task.
