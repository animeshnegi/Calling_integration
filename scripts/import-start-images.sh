#!/usr/bin/env bash
set -euo pipefail

ARCHIVE="${1:?Usage: $0 IMAGE.tar.gz IMAGE_TAG}"
TAG="${2:?Usage: $0 IMAGE.tar.gz IMAGE_TAG}"

command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
[[ -f .env ]] || { echo "Missing .env; copy .env.example and configure it first" >&2; exit 1; }
[[ -f "$ARCHIVE" ]] || { echo "Archive not found: $ARCHIVE" >&2; exit 1; }
if [[ -f "$ARCHIVE.sha256" ]]; then
  # Checksums use basenames so a Windows export can be moved to any VM folder.
  (cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "$ARCHIVE").sha256")
fi

# docker load accepts both the native Windows .tar and compressed .tar.gz.
docker image load --input "$ARCHIVE"
export IMAGE_TAG="$TAG"
docker network inspect crm-network >/dev/null 2>&1 || docker network create crm-network
docker compose up -d --no-build
docker compose ps

printf '\nImages started without building. This does not install host scripts or nginx routes.\n'
printf 'For a missing softphone installer, extract the small host-tools archive; docker load does not copy host scripts.\n'
printf 'See deploy/SOFTPHONE_TOOLS.md and deploy/RELEASE_CHECKLIST.md (included in the host-tools archive).\n'
printf 'After extracting host tools: python3 scripts/finish-deployment.py %s YOUR_WEB_DOMAIN\n' "$TAG"
