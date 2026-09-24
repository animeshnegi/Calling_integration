#!/usr/bin/env bash
set -euo pipefail

TAG="${1:-$(date -u +%Y%m%d-%H%M%S)}"
PLATFORM="${PLATFORM:-linux/amd64}"
OUTPUT="${OUTPUT:-engineerip-telephony-${TAG}.tar.gz}"

command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
export IMAGE_TAG="$TAG" DOCKER_DEFAULT_PLATFORM="$PLATFORM"

echo "Building $PLATFORM images with tag $TAG..."
docker compose build --pull

echo "Exporting $OUTPUT..."
docker image save "engineerip/telephony-api:${TAG}" "engineerip/asterisk:${TAG}" | gzip -1 > "$OUTPUT"
sha256sum "$OUTPUT" > "$OUTPUT.sha256"
printf '\nCreated:\n  %s\n  %s.sha256\n\nOn the VM, copy both files and run:\n  ./scripts/import-start-images.sh %s %s\n' "$OUTPUT" "$OUTPUT" "$OUTPUT" "$TAG"
