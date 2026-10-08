# Codex: Camera Eye final release hardening, tests and deployment

You have full access to `koushikdatascience-netizen/camera-automation`. Work on branch `feat/camera-eye-live-view-ux`. The product is a Windows Camera Eye edge EXE + PostgreSQL cloud portal + SnapKey CRM integration. User wants to deliver urgently. **Implement and TEST remaining gaps; do not merely describe them.** Do not merge/deploy or execute real employee CRM mutations without explicit approval.

## Source truth
Read `docs/CRM_PERSON_WISE_ATTENDANCE_INTEGRATION_V2.md`, `docs/CRM_PERSON_POLICY_V2_API.md`, `cloud_portal/person_attendance_rules.py`, `cloud_portal/attendance_tokens.py`, `cloud_portal/working_time.py`, `cloud_portal/crm_client.py`, `cloud_portal/api.py`, `cloud_portal/postgres_storage.py`, and tests. Existing legacy API must remain backward compatible.

CRM:
- POST `/api/Auth/loginUsingFaceTenant` with CRM-enrolled base64Image + tenantId: returns fresh employee token without changing attendance. Token lasts 24h (also obey JWT exp).
- POST `/api/UserRoster/LoginLogout` with userId/date/actualStartTime for check-in or actualOffTime for normal checkout.
- POST `/api/UserActivity/auto-logout` with userId/remarks for 60-minute absence ONLY.
Never interchange these APIs. Never commit JWTs or face images.

## CRITICAL issues to audit/fix
1. **Worker correctness:** currently `_evaluate_v2_person_absences` records transitions and feature-gated CRM mutation. Test sixth distinct grace episode, overnight boundaries, delayed heartbeat, no camera coverage, manual mode, on-break, rapid reappearance, duplicate ticks, multiworker concurrency and max logoff. Evaluate true per-camera coverage, not any unrelated entrance camera.
2. **Token security:** `CAMERA_EYE_TOKEN_ENCRYPTION_KEY` Fernet key required before enabling CRM auto logout; provide secure rotation/recovery runbook, no token leaks, and verify `Authorization: Bearer ...` handling. JWT payload parsing is expiry hint, not cryptographic verification. Ensure tenant and CRM user identity match.
3. **External idempotency:** `crm_auto_logout_actions` claim table is local idempotency only. A timeout can mean remote success; reconcile with CRM before retry, don't blindly resubmit. Ensure CRM 200 JSON success criteria are tested against actual contract. Ensure 401 token refresh does not duplicate attendance.
4. **Notifications:** `_notify_cloud_event` uses legacy shop recipients; implement per-person routing/preferences, durable delivery outbox, retries, rate limiting, dedup and delivery status. Test SMTP and WhatsApp Business with mocks; do not send real messages without approval.
5. **Evidence:** secure 3 snapshots + short clip at each action where available. Explicitly record missing evidence and cause. Expose signed, scoped media URLs or authenticated proxy; never return filesystem paths or unrestricted media.
6. **Daily summaries:** current `working_time.py` handles closed intervals only; implement cross-midnight shifts, open sessions, exact break labels (verify existing activity type names), day-end cutoff, total working hours, absence/paid-break policy and report. Preserve raw evidence/events.
7. **Authz:** current `X-CRM-Integration-Key` is a shared key; enforce tenant/shop scope, audit mutations, and never expose integration keys to browser. Verify all read endpoints.
8. **Database:** migration/backup, indexes, rollback, PostgreSQL transaction boundaries, action locks and tenant isolation. Verify that new `CREATE TABLE` statements work in a fresh database and upgrades.
9. **Testing:** run compileall, pytest unit/integration tests, staging PostgreSQL tests, FastAPI TestClient, mocked CRM responses and notifications, Windows edge packaging smoke test and API contract tests. Fix failures, report actual commands/results. Verify no production credentials in test logs.
10. **Deployment:** create release notes, environment example (no secrets), Docker staging instructions, exact rollback, and CRM handoff docs. Keep `CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED=false` until end-to-end test and explicit approval.

## Initial commands

```powershell
git fetch origin
git checkout feat/camera-eye-live-view-ux
python -m compileall -q cloud_portal
python -m pytest -q tests/test_person_attendance_rules.py tests/test_working_time.py tests/test_attendance_tokens.py
```

## Required final report

Provide: actual commit SHA, tests executed and pass/fail, endpoint/payload list, DB migration changes, env vars, screenshot/video evidence behavior, CRM mutation mock test results, notification test results, known limitations, staging readiness, deployment commands and rollback commands. **Never claim fully production ready if tests or external contracts remain unverified.**
