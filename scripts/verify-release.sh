#!/usr/bin/env bash
# Read-only deployment verification. No image builds, container restarts,
# nginx changes, provider API calls, or credential display.
set -euo pipefail

TAG="${1:?Usage: verify-release.sh IMAGE_TAG [WEB_DOMAIN]}"
export IMAGE_TAG="$TAG"
DOMAIN="${2:-tel.engineerip.com}"
DIR="${REMOTE_DIR:-/opt/Calling_integration}"
IMAGE="engineerip/telephony-api:${TAG}"

cd "$DIR"
printf '== 1/6: loaded API image and VM architecture ==\n'
docker image inspect "$IMAGE" >/dev/null
image_arch="$(docker image inspect "$IMAGE" --format '{{.Architecture}}')"
host_arch="$(uname -m)"
case "$host_arch" in
  x86_64|amd64) expected_arch=amd64 ;;
  aarch64|arm64) expected_arch=arm64 ;;
  *) expected_arch="$host_arch" ;;
esac
[ "$image_arch" = "$expected_arch" ] || {
  printf 'FAIL: image is %s but VM is %s. Build/load the matching platform.\n' "$image_arch" "$host_arch" >&2
  exit 1
}
printf 'PASS: %s is %s and matches %s.\n' "$IMAGE" "$image_arch" "$host_arch"

printf '\n== 2/6: offline voicemail transcription in the imported image ==\n'
# --network none proves the recognizer needs no speech API or model download.
docker run --rm --network none "$IMAGE" python \
  /app/deploy-kit/scripts/check-voicemail-transcription.py

printf '\n== 3/6: public SIP WebSocket upgrade ==\n'
python3 scripts/check-softphone-websocket.py "wss://${DOMAIN}/ws"

printf '\n== 4/6: fresh admin UI after image replacement ==\n'
curl -fsSI "https://${DOMAIN}/admin-assets/admin.js" | tr -d '\r' \
  | grep -Eqi '^cache-control:.*no-store' || {
    printf 'FAIL: admin.js is still cacheable or not served by the new API image.\n' >&2
    exit 1
  }
printf 'PASS: admin.js is served with no-store. Hard-refresh the browser once.\n'

printf '\n== 5/6: running API and ARI services ==\n'
docker compose ps telephony-api telephony-ari

printf '\n== 6/6: recent ARI/voicemail worker log tail ==\n'
docker compose logs --tail=20 telephony-ari

cat <<'EOF'

PASS: image, offline transcription, WebSocket transport, UI freshness and
services. This script does NOT prove SendGrid authentication, inbox delivery,
SIP registration, internal 101↔105 audio, or the absence of an Asterisk crash.
Check those separately in deploy/RELEASE_CHECKLIST.md.
EOF
