# Face-template provenance and isolated QA

## Behavior and compatibility

Local enrollment previously stored no model key, while diagnostics and candidate
selection relied on personnel-level CRM metadata. New enrollments now store a
per-template `model_key` atomically with the embedding. The fingerprint binds the
actual buffalo_l detector and recognizer weights, CPU execution, detector size,
and enrollment settings. The loaded model must still match the files before and
after enrollment or matching. Changing weights requires a recognition-service
restart and genuine re-enrollment where fingerprints differ.

Weight hashes are cached by resolved paths, size, modification/change timestamps
and file identity. Normal checks stat the files; they do not rehash weights for
every face/frame. Missing files fail closed. This is an integrity check for trusted
deployment files, not protection against an administrator replacing the process
or tampering with the operating system.

Each local/cloud diagnostic exposes `templates`, `template_status_counts`, and
`model_compatibility`, keeping existing identity/count fields. Aggregate priority
is UNVERIFIED, MODEL_UNAVAILABLE, INCOMPATIBLE, COMPATIBLE; counts always show mixed
states. No templates means UNVERIFIED. Cloud status compares cloud weights;
`edge_reports` separately describe the last edge heartbeat, not current live proof.

| Status | Operator action | Recognition |
| --- | --- | --- |
| COMPATIBLE | Template matches the validated model | Eligible if vector is valid and employee active |
| INCOMPATIBLE | Re-enroll using the intended matching model | Excluded |
| MODEL_UNAVAILABLE | Restore verified model files and restart | Excluded |
| UNVERIFIED | Re-enroll; do not stamp a key onto legacy vectors | Excluded |

512-dimensional, finite, nonzero vectors are required. Ambiguous employee matches
still fail safely. Image previews do not prove template compatibility. Personnel
lists/diagnostics contain neither face Base64 nor embeddings.

## Storage and trusted synchronization

SQLite initialization adds nullable `face_profiles.model_key` and an index on
`(model_key,person_id)`. It does not backfill legacy rows. Cloud SQLite/PostgreSQL
already have nullable `cloud_face_profiles.model_key` from the preceding release;
this change inserts that key in the same transaction as the template.

Only server-generated embeddings from CRM enrollment images receive model keys.
CRM vector/model claims are not trusted. Scoped, authenticated edge configuration
transmits a key per face; personnel-level metadata alone cannot verify a template.
The edge validates scope, identity, vector and model before committing a snapshot.
An existing template ID cannot be reassigned, have its embedding overwritten, or
have missing/different provenance upgraded by an incoming claim: genuine
regeneration requires a new template ID. Verified original provenance is retained.

Legacy CRM cache entries remain unverified. Refresh regenerates from the CRM image
into a separately identified verified template, preserving legacy bytes. Obsolete
verified CRM templates retain the existing revocation behavior; legacy/local-image
templates are retained and never become candidates merely because models exist.
Person deactivation and attendance/history reconciliation semantics remain intact.
Corrupt verified cache entries fail refresh and require explicit re-enrollment;
they are not silently overwritten. No automatic legacy-vector trust switch exists.

## QA on 127.0.0.1:8092

The existing QA employee `QA-EMP-001` and its face were inspected read-only only.
Its database has no per-template key column yet. Leave that employee and enrollment
unchanged: after additive migration its existing face must show UNVERIFIED.

1. Stop only the terminal running the isolated QA server with Ctrl+C. Do not stop
   the installed application or any cloud service. If unsure which process owns
   8092, inspect `Get-NetTCPConnection -LocalPort 8092` and the matching process
   command line before stopping anything.
2. Make a SQLite-consistent backup and restart, in PowerShell:

```powershell
Set-Location 'D:\Madhushala Software\Camera ai\camera-automation'
@'
import os, sqlite3
from pathlib import Path
from datetime import datetime
root=Path(os.environ['TEMP'])/'CameraEye-Personnel-QA'
source=sqlite3.connect((root/'camera_qa.db').as_uri()+'?mode=ro',uri=True)
backup=root/('camera_qa_pre_provenance_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.db')
target=sqlite3.connect(backup)
source.backup(target)
target.close();source.close()
print('QA backup created:',backup)
'@ | python -
$env:CAMERA_AUTOMATION_CONFIG=Join-Path $env:TEMP 'CameraEye-Personnel-QA\config.yaml'
python -m uvicorn camera_service.api:app --host 127.0.0.1 --port 8092
```

Use the existing QA configuration with cloud synchronization disabled. Startup
performs an additive schema migration only; never point this test at production.

3. Open `http://127.0.0.1:8092/setup`, Personnel. Create a separate synthetic
   employee `QA-EMP-002` and, with the operator's consent, enroll a clear face.
   Do not re-enroll/remove QA-EMP-001. The new employee must show one COMPATIBLE
   template and its thumbnail. The old employee stays UNVERIFIED.
4. Inspect the new employee's `sync_diagnostic` using the local personnel API;
   it must show COMPATIBLE=1, other counts zero, and a per-template key. Ordinary
   personnel responses must contain no vector or Base64 image. Test recognition
   only in this isolated setup; no live CRM attendance requests are needed.
5. Keep new QA enrollment/evidence out of Git. To roll back the QA run, stop that
   terminal and preserve both databases; do not overwrite the original or delete
   history. Old application code can tolerate the additive column, but has weaker
   recognition safety and must not be used as a production mitigation.

## Verification and release boundaries

Focused and full pytest results are saved under `artifacts/provenance-*.xml`.
Final results: focused 67 passed / 0 failed / 0 skipped (28.90 seconds); full
376 passed / 0 failed / 0 skipped (112.97 seconds), including all 9 PostgreSQL
integration tests. The final full run also covers the additional immutable-import
and model-change regressions. Python compileall, cloud JavaScript and extracted
local setup JavaScript syntax checks, and `git diff --check` passed. Existing
FastAPI/Starlette deprecation warnings remain.

Commands executed (each run used a unique temporary pytest directory):

```powershell
python -m pytest -q tests/test_face_model_provenance.py tests/test_personnel_sync_previews.py tests/test_crm_enrollment_cache.py tests/test_attendance_policy_and_reconciliation.py --basetemp=$testTemp --junitxml=artifacts/provenance-focused.xml --tb=short
python -m pytest -q tests --basetemp=$testTemp --junitxml=artifacts/provenance-full-suite-final.xml --tb=short
python -m compileall -q camera_service cloud_portal
node --check cloud_portal/static/js/main.js
git diff --check
```

The full run required `SNAPKEY_REQUIRE_POSTGRES_TESTS=1` and a test-only DSN for
the existing Docker test project `camera-eye-personnel-tests`, PostgreSQL 16 on
`127.0.0.1:55432`. The test service is stopped after verification without deleting
its data. Do not substitute production credentials.

Tests use synthetic images/templates, mocked CRM operations, and unique schemas
in the loopback-only `camera_eye_test` PostgreSQL database. Real CRM authentication,
physical-camera recognition and browser visual QA are not claimed for this task.

The change is committed locally on `codex/face-model-provenance-hardening-20261009`.
No production deployment, push, CI/CD dispatch, installer build, production data
mutation, activation change or automatic-logout flag change is authorized here.
Later release review must align cloud and edge model fingerprints and plan genuine
legacy re-enrollment before enabling recognition for client delivery.
