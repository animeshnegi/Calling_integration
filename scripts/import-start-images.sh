#!/usr/bin/env bash
set -euo pipefail

ARCHIVE="${1:?Usage: $0 IMAGE.tar.gz IMAGE_TAG}"
TAG="${2:?Usage: $0 IMAGE.tar.gz IMAGE_TAG}"

command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
[[ -f .env ]] || { echo "Missing .env; copy .env.example and configure it first" >&2; exit 1; }
[[ -f "$ARCHIVE" ]] || { echo "Archive not found: $ARCHIVE" >&2; exit 1; }
if [[ -f "$ARCHIVE.sha256" ]]; then sha256sum -c "$ARCHIVE.sha256"; fi

gzip -dc "$ARCHIVE" | docker image load
export IMAGE_TAG="$TAG"
docker network inspect crm-network >/dev/null 2>&1 || docker network create crm-network
docker compose up -d --no-build
docker compose ps
