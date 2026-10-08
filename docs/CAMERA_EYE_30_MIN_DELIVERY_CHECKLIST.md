# Camera Eye — urgent delivery checklist (2026-10-08)

## Release decision

**NOT PRODUCTION READY.** This branch contains new code that has not been executed in CI/staging. Do not deploy directly to a live client or promise that all requested workflows work.

## Implemented in source

- Person-specific PostgreSQL policy CRUD (integration/v2) with 24-hour-token flow specified but not implemented.
- Existing CRM LoginLogout remains the normal check-in/out API.
- CRM auto-logout adapter exists separately; it is **not called** by the V2 absence evaluator.
- Presence worker evaluates person policies, skips unhealthy camera coverage and persists idempotent absence transitions.
- CRM read endpoints for presence, activity, daily summary, alerts and evidence manifests.
- Gross/net/break calculations for closed sessions.

## Blockers

1. No encrypted face-token storage, 24-hour expiry/refresh, or CRM auto-logout job idempotency.
2. No verified SMTP/WhatsApp dispatch for V2 transitions.
3. Evidence endpoint returns manifests, not signed media.
4. Daily summary is provisional for open/cross-midnight shifts and absence/payroll deductions.
5. Missing production authz enforcement for per-tenant CRM integration keys.
6. Tests committed but not executed; no full end-to-end tests.
7. PostgreSQL schema creation runs on service startup; must stage and backup first.

## Safe validation on a development checkout

```powershell
git fetch origin
git checkout feat/camera-eye-live-view-ux
python -m compileall -q cloud_portal
python -m pytest -q tests/test_person_attendance_rules.py tests/test_working_time.py
```

Then start a **separate staging PostgreSQL instance** and the API with staging CRM credentials. Test policy PUT/GET, camera heartbeat, checked-in presence, 5/15/60 minute transitions, duplicate worker ticks, camera offline, break, sixth episode, alerts, and daily summaries. Inspect database records and ensure no production CRM auto-logout calls are made.

## CRM API separation

- `/api/Auth/loginUsingFaceTenant`: get/refresh employee token; no attendance mutation.
- `/api/UserRoster/LoginLogout`: normal attendance check-in/out with actualStartTime/actualOffTime.
- `/api/UserActivity/auto-logout`: absence-specific logout with userId and remarks; adapter only pending safe worker integration.

## Operational recommendation

Demonstrate the V2 policy and read endpoints on staging and clearly label missing workflows. Ship to production only after the blockers are resolved and the test matrix passes.
