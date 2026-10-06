#!/usr/bin/env bash
# Exercises the real deployment handler with fake Docker/HTTP commands only.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/bin" "$work/project" "$work/state"
touch "$work/project/.env"
printf 'services:\n  portal:\n    image: old\n' > "$work/project/docker-compose.cloud.yml"
printf 'services:\n  portal:\n    image: new\n' > "$work/release-compose.yml"
sed -e "s|project=/opt/camera-eye|project=$work/project|" \
    -e "s|state=/var/lib/camera-eye-ci|state=$work/state|" \
    -e 's/{1..60}/{1..2}/' \
    "$root/tools/deploy_cloud.sh" > "$work/deploy.sh"
export MOCK_DIR="$work" REVISION=0123456789012345678901234567890123456789
export PATH="$work/bin:$PATH"
cat > "$work/bin/docker" <<'MOCK'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$MOCK_DIR/docker.log"
if [[ "$1" == load ]]; then cat >/dev/null; exit; fi
if [[ "$1" == image ]]; then
  if [[ "$*" == *org.opencontainers.image.revision* ]]; then
    [[ ${SCENARIO:-} != mismatch ]] && echo "$REVISION" || echo wrong
  else echo sha256:new; fi
elif [[ "$1" == inspect ]]; then
  if [[ "$*" == *org.opencontainers.image.revision* ]]; then echo old-revision
  elif [[ -f "$MOCK_DIR/new" ]]; then echo sha256:new
  else echo sha256:old; fi
elif [[ "$1" == compose ]]; then
  if [[ "$*" == *' ps -q '* ]]; then echo container
  elif [[ "$*" == *' up '* ]]; then
    override=''
    while (( $# )); do
      if [[ "$1" == -f ]]; then shift; override=$1; fi
      shift
    done
    if grep -q 'sha256:old' "$override"; then rm -f "$MOCK_DIR/new"
    else touch "$MOCK_DIR/new"; fi
  fi
else exit 1; fi
MOCK
cat > "$work/bin/curl" <<'MOCK'
#!/usr/bin/env bash
if [[ ${SCENARIO:-} == failure && -f "$MOCK_DIR/new" ]]; then exit 22; fi
printf '{"status":"ok","service":"snapkey-portal"}\n'
MOCK
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/bin/sleep"
printf '#!/usr/bin/env bash\nexit 0\n' > "$work/bin/flock"
chmod +x "$work/bin/"*

if SSH_ORIGINAL_COMMAND='uname -a' bash "$work/deploy.sh" 2>/dev/null; then
  echo 'Unrestricted SSH command was accepted' >&2; exit 1
fi
make_bundle() {
  local payload="$work/payload"
  rm -rf "$payload"; mkdir -p "$payload"
  cp "$work/release-compose.yml" "$payload/docker-compose.cloud.yml"
  printf image | gzip > "$payload/image.tar.gz"
  tar -C "$payload" -czf "$work/release.tar.gz" docker-compose.cloud.yml image.tar.gz
}
make_bundle
if SCENARIO=mismatch SSH_ORIGINAL_COMMAND="deploy $REVISION" bash "$work/deploy.sh" < "$work/release.tar.gz" 2>/dev/null; then
  echo 'Mismatched image revision was accepted' >&2; exit 1
fi
test ! -f "$work/new"
SSH_ORIGINAL_COMMAND="deploy $REVISION" bash "$work/deploy.sh" < "$work/release.tar.gz"
test "$(cat "$work/state/current-revision")" = "$REVISION"
test "$(cat "$work/state/previous-image")" = sha256:old
grep -q 'image: new' "$work/project/docker-compose.cloud.yml"
rm "$work/new"
if SCENARIO=failure SSH_ORIGINAL_COMMAND="deploy $REVISION" bash "$work/deploy.sh" < "$work/release.tar.gz"; then
  echo 'Unhealthy deployment was accepted' >&2; exit 1
fi
test ! -f "$work/new"
grep -q 'image: new' "$work/project/docker-compose.cloud.yml"
if grep -E ' down|volume rm|system prune' "$work/docker.log"; then
  echo 'Deployment attempted a destructive Docker operation' >&2; exit 1
fi
echo 'Deployment validation passed: restricted command, revision check, success, rollback.'
