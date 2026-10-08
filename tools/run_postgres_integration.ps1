$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$composeFile = Join-Path $repoRoot "docker-compose.cloud.yml"
$envExample = Join-Path $repoRoot ".env.cloud.example"
$composeArgs = @("compose", "--env-file", $envExample, "-f", $composeFile, "--profile", "integration-test")
$testExitCode = 1

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop must be running to start the isolated PostgreSQL test container."
}

try {
    & docker @composeArgs up -d postgres-test
    if ($LASTEXITCODE -ne 0) { throw "Could not start the isolated PostgreSQL test service." }

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        & docker @composeArgs exec -T postgres-test pg_isready -U camera_eye_test -d camera_eye_test *> $null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw "The isolated PostgreSQL test service did not become ready." }

    $env:SNAPKEY_TEST_DATABASE_URL = "postgresql+psycopg://camera_eye_test:camera-eye-test-only@127.0.0.1:55432/camera_eye_test"
    $env:SNAPKEY_REQUIRE_POSTGRES_TESTS = "1"
    & python -m pytest -q (Join-Path $repoRoot "tests/test_postgres_attendance_integration.py")
    $testExitCode = $LASTEXITCODE
}
finally {
    & docker @composeArgs rm -sf postgres-test *> $null
}

exit $testExitCode
