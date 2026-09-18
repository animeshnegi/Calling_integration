#!/usr/bin/env bash
set -euo pipefail

CONTAINERS=(engineerip-telephony-api engineerip-telephony-ari engineerip-asterisk)
APPLY=false
[[ "${1:-}" == "--apply" ]] && APPLY=true

command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }

echo "Telephony container log usage:"
for container in "${CONTAINERS[@]}"; do
  if ! docker inspect "$container" >/dev/null 2>&1; then
    printf '  %-32s %s\n' "$container" "not found"
    continue
  fi
  path=$(docker inspect --format '{{.LogPath}}' "$container" 2>/dev/null || true)
  if [[ -n "$path" && -e "$path" ]]; then
    size=$(sudo du -h "$path" 2>/dev/null | awk '{print $1}' || echo unknown)
    printf '  %-32s %s (%s)\n' "$container" "$size" "$path"
  else
    printf '  %-32s %s\n' "$container" "log path unavailable for this driver"
  fi
done

echo
echo "Docker disk summary:"
docker system df || true

if ! $APPLY; then
  cat <<'EOF'

Dry run only. To immediately truncate existing telephony container logs:
  ./scripts/cleanup-logs.sh --apply

This script does not delete images, containers, recordings, voicemail, databases,
or Docker volumes. Install the updated compose file and recreate containers to
make automatic 10 MB x 3-file rotation permanent.
EOF
  exit 0
fi

echo
read -r -p "Truncate current telephony logs now? Type YES: " answer
[[ "$answer" == "YES" ]] || { echo "Cancelled."; exit 1; }

for container in "${CONTAINERS[@]}"; do
  docker inspect "$container" >/dev/null 2>&1 || continue
  path=$(docker inspect --format '{{.LogPath}}' "$container" 2>/dev/null || true)
  if [[ -n "$path" && -e "$path" ]]; then
    sudo truncate -s 0 "$path"
    echo "Truncated Docker log for $container"
  fi
done

# Older Asterisk images may also write messages/security logs inside the
# container layer. Clear only files under Asterisk's own log directory.
if docker inspect engineerip-asterisk >/dev/null 2>&1; then
  docker exec --user root engineerip-asterisk sh -c \
    'find /var/log/asterisk -type f -exec truncate -s 0 {} \;' 2>/dev/null || \
    echo "Could not clear internal Asterisk logs (container may be stopped)."
fi

echo "Cleanup complete. Re-run this script without --apply to verify usage."
