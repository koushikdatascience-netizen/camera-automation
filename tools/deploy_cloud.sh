#!/usr/bin/env bash
# Install as /usr/local/sbin/camera-eye-ci-deploy, root-owned and mode 0755.
# A forced-command SSH key may invoke only: deploy <40-character commit SHA>.
set -euo pipefail
umask 077

if [[ ! ${SSH_ORIGINAL_COMMAND:-} =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
  echo 'Only deploy <commit SHA> is accepted.' >&2
  exit 64
fi
revision=${BASH_REMATCH[1]}
image="camera-eye-portal:$revision"
project=/opt/camera-eye
state=/var/lib/camera-eye-ci
mkdir -p "$state"
exec 9>"$state/deploy.lock"
flock -w 1800 9
work=$(mktemp -d "$state/release.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT

test -f "$project/.env"
test -f "$project/docker-compose.cloud.yml"
compose=(docker compose --project-name camera-eye --project-directory "$project"
  --env-file "$project/.env" -f "$project/docker-compose.cloud.yml")
container=$("${compose[@]}" ps -q portal)
test -n "$container"
previous_image=$(docker inspect --format '{{.Image}}' "$container")
previous_revision=$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$container")

# Read the image directly from SSH; no repository token or server secrets leave the host.
gzip -dc | docker load
actual_revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")
if [[ "$actual_revision" != "$revision" ]]; then
  echo 'Image revision does not match the requested commit.' >&2
  exit 1
fi
expected_image=$(docker image inspect --format '{{.Id}}' "$image")
override="$work/image.yml"
write_override() {
  printf 'services:\n  portal:\n    image: "%s"\n    pull_policy: never\n' "$1" > "$override"
}
wait_healthy() {
  local expected=$1 current
  for attempt in {1..60}; do
    current=$("${compose[@]}" -f "$override" ps -q portal)
    if [[ -n "$current" ]] && [[ $(docker inspect --format '{{.Image}}' "$current") == "$expected" ]] \
      && curl -fsS --max-time 3 http://127.0.0.1:8000/health 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("status")=="ok" and d.get("service")=="snapkey-portal"' 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}
rollback() {
  echo 'Deployment failed; restoring the previous portal image.' >&2
  write_override "$previous_image"
  if "${compose[@]}" -f "$override" up -d --no-deps --no-build --pull never portal \
    && wait_healthy "$previous_image"; then
    echo 'Previous portal image restored.' >&2
  else
    echo 'ROLLBACK FAILED: manual intervention required.' >&2
  fi
}

write_override "$image"
# SIGTERM/SSH interruption must attempt rollback too. No database/volume deletion.
trap 'trap - HUP INT TERM; rollback; exit 1' HUP INT TERM
if "${compose[@]}" -f "$override" up -d --no-deps --no-build --pull never portal \
  && wait_healthy "$expected_image"; then
  printf '%s\n' "$previous_image" > "$state/previous-image"
  printf '%s\n' "$previous_revision" > "$state/previous-revision"
  printf '%s\n' "$revision" > "$state/current-revision"
  cp "$override" "$state/current-image.yml"
  echo "Portal healthy at commit $revision"
else
  rollback
  exit 1
fi
