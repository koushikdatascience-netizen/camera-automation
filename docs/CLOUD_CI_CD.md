# Cloud CI/CD

The deployment branch is `feat/camera-eye-live-view-ux`, matching the current server checkout. Pull requests and pushes to this branch or `main` run tests. Only pushes/manual runs on the deployment branch can deploy. Do not merge deployment changes into another branch expecting a production release without updating the branch condition.

## Flow

1. Run all unit/contract tests, Python/JavaScript syntax checks and the production source smoke on Linux and Windows, using isolated data and fake inference backends.
2. Build a Docker image labelled with the exact tested commit.
3. Start that image against a temporary PostgreSQL 17 service and verify portal health and SQL connectivity.
4. Stream the same image over host-key-verified SSH to the deployment handler.
5. Replace only the portal service. Keep PostgreSQL, evidence volumes, face models and `/opt/camera-eye/.env` in place.
6. Confirm the running image ID and portal health. On failure, restore the prior image and fail the workflow. Deployments are serialized in GitHub and with a server file lock.

The workflow does not build the Windows installer or validate physical cameras, face accuracy, real CRM mutations, or WebRTC. Green CI does not resolve the application defects documented by the project review. The CRM test records the currently deployed payload shape; it is not an independent verification of the upstream business contract.

## One-time setup

- Install `tools/deploy_cloud.sh` as root-owned `/usr/local/sbin/camera-eye-ci-deploy`, mode `0755` (LF line endings).
- Use a dedicated SSH key. Its authorized-keys entry must be prefixed with `restrict,command="/usr/local/sbin/camera-eye-ci-deploy"`. This prevents this key from opening an interactive shell or forwarding ports. Deploying an application image still grants powerful access: limit repository write access accordingly.
- Configure repository or production-environment secrets: `CAMERA_DEPLOY_HOST`, `CAMERA_DEPLOY_USER`, `CAMERA_DEPLOY_SSH_KEY`, and `CAMERA_DEPLOY_KNOWN_HOSTS` (verified OpenSSH host-key line). Never put a password/private key or `.env` in Git.
- Docker Compose v2, `flock`, `gzip`, `curl`, Python 3, and an existing healthy `camera-eye` Compose project are required on the host. The existing portal must be running for a first deployment to capture its rollback image.
- The `production` environment must allow this branch and must not require manual approval if fully automatic deployment is wanted.

The server uses `/opt/camera-eye/docker-compose.cloud.yml` and its existing `.env`. Application releases arrive as images; `/opt/camera-eye` is not automatically reset or pulled. Changing server infrastructure/Compose settings requires a separate reviewed update. The running revision is recorded in the image label and `/var/lib/camera-eye-ci/current-revision`.

## Rollback and operations

The previous image is retained; no image pruning occurs during deployment. The handler records `/var/lib/camera-eye-ci/previous-image` and `current-image.yml`. To reapply the current deployment manually, include the saved override with the existing Compose file and project name:

```bash
docker compose --project-name camera-eye --project-directory /opt/camera-eye \
  --env-file /opt/camera-eye/.env \
  -f /opt/camera-eye/docker-compose.cloud.yml \
  -f /var/lib/camera-eye-ci/current-image.yml \
  up -d --no-deps --no-build --pull never portal
```

Rollback restores the application image only. Future schema changes must remain backward-compatible; this mechanism cannot undo destructive database migrations. Monitor disk use and retain needed rollback images before separately pruning old releases.

## Baseline snapshot

On 5 October 2026, the Git-tracked server project (149 files) and running application (86 files) were downloaded into ignored `data/server-snapshot-20261005/`. Both matched commit `3bdf77c2dc29c5994276888584dff1cf45a41c52`. Production secrets and runtime data remain on the server. `verification.json` records archive hashes and comparison results.
