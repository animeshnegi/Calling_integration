#!/usr/bin/env bash
set -euo pipefail

TAG="${1:-$(date -u +%Y%m%d-%H%M%S)}"
PLATFORM="${PLATFORM:-linux/amd64}"
OUTPUT="${OUTPUT:-engineerip-telephony-${TAG}.tar.gz}"

for tool in docker gzip tar sha256sum mktemp; do
  command -v "$tool" >/dev/null || { echo "$tool is required (use Bash/WSL or Git Bash)" >&2; exit 1; }
done
TOOLS_OUTPUT="${TOOLS_OUTPUT:-engineerip-softphone-tools-${TAG}.tar.gz}"
TOOLS_CONTAINER=""
TOOLS_DIR=""
cleanup() {
  if [[ -n "$TOOLS_CONTAINER" ]]; then docker rm "$TOOLS_CONTAINER" >/dev/null 2>&1 || true; fi
  if [[ -n "$TOOLS_DIR" ]]; then rm -rf -- "$TOOLS_DIR"; fi
}
trap cleanup EXIT
export IMAGE_TAG="$TAG" DOCKER_DEFAULT_PLATFORM="$PLATFORM"

echo "Building $PLATFORM images with tag $TAG..."
docker compose build --pull

echo "Exporting $OUTPUT..."
docker image save "engineerip/telephony-api:${TAG}" "engineerip/asterisk:${TAG}" | gzip -1 > "$OUTPUT"
(cd "$(dirname "$OUTPUT")" && sha256sum "$(basename "$OUTPUT")") > "$OUTPUT.sha256"
# Export the host tools too: docker save/load alone never updates host files.
# Use a stopped container, so no application or PBX process runs on the builder.
TOOLS_DIR="$(mktemp -d)"
TOOLS_CONTAINER="$(docker create "engineerip/telephony-api:${TAG}")"
TOOLS_COPY_DIR="$TOOLS_DIR/"
if command -v cygpath >/dev/null; then TOOLS_COPY_DIR="$(cygpath -w "$TOOLS_DIR")"; fi
# Git Bash must not rewrite the Linux path inside the container argument.
MSYS_NO_PATHCONV=1 docker cp "${TOOLS_CONTAINER}:/app/deploy-kit/." "$TOOLS_COPY_DIR"
tar -czf "$TOOLS_OUTPUT" -C "$TOOLS_DIR" \
  scripts/finish-deployment.py scripts/install-softphone-proxy.py \
  scripts/check-softphone-websocket.py scripts/check-voicemail-transcription.py \
  scripts/verify-release.sh scripts/diagnose-extension-audio.sh \
  deploy/nginx/softphone-websocket.conf deploy/SOFTPHONE_TOOLS.md \
  deploy/RELEASE_CHECKLIST.md deploy/THIRD_PARTY_NOTICES.md \
  deploy/fixtures/README.md deploy/fixtures/goforward-8k.wav
(cd "$(dirname "$TOOLS_OUTPUT")" && sha256sum "$(basename "$TOOLS_OUTPUT")") > "$TOOLS_OUTPUT.sha256"
printf '\nCreated image archive:\n  %s\n  %s.sha256\nHost tools (COPY THIS TOO):\n  %s\n  %s.sha256\n' "$OUTPUT" "$OUTPUT" "$TOOLS_OUTPUT" "$TOOLS_OUTPUT"
printf '\nOn the VM, use --no-build as before. Extract the tools archive into /opt/Calling_integration, then run the proxy installer there.\nSee deploy/SOFTPHONE_TOOLS.md. Loading the images alone does NOT update host scripts or nginx.\n'
printf '\nExisting import helper (if present):\n  bash scripts/import-start-images.sh %s %s\n' "$OUTPUT" "$TAG"
