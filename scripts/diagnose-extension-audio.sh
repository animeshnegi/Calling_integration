#!/usr/bin/env bash
# Read-only incident snapshot. Never restarts/reloads Asterisk, places calls,
# changes codecs, enables debug logging, or prints Docker environments/.env.
# Existing Docker logs can contain SIP traces: review/redact before sharing.
set -euo pipefail
CONTAINER="${1:-engineerip-asterisk}"
SINCE="${2:-2h}"
command -v docker >/dev/null || { echo 'Docker is required on the PBX host.' >&2; exit 1; }
docker inspect "$CONTAINER" >/dev/null 2>&1 || { echo 'Cannot inspect the Asterisk container; check its name and Docker access.' >&2; exit 1; }

section() { printf '\n===== %s =====\n' "$1"; }
collect() {
  if "$@"; then return; else
    local status=$?
    printf '[diagnostic command failed with status %s; continuing the snapshot]\n' "$status" >&2
  fi
}
section 'UTC snapshot time (compare with the failed 101 -> 105 call)'
date -u '+%Y-%m-%dT%H:%M:%SZ'
section 'Container state: a running ExitCode=0 does NOT describe a previous exit'
collect docker inspect --format 'Image={{.Image}} Status={{.State.Status}} Running={{.State.Running}} ExitCode={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} RestartCount={{.RestartCount}} StartedAt={{.State.StartedAt}} FinishedAt={{.State.FinishedAt}} Error={{.State.Error}}' "$CONTAINER"
section "Docker exit/restart/OOM events since $SINCE (daemon retains a bounded history)"
# A fixed --until is essential: this is a finite snapshot, not a hanging watch.
UNTIL="$(date -u '+%Y-%m-%dT%H:%M:%S.%NZ')"
collect docker events --since "$SINCE" --until "$UNTIL" \
  --filter type=container --filter "container=$CONTAINER" \
  --filter event=die --filter event=restart --filter event=oom --filter event=start \
  --format '{{.Time}} action={{.Action}} exitCode={{index .Actor.Attributes "exitCode"}}'
section 'Verified image portability (missing file means an older/unverified image)'
collect docker exec "$CONTAINER" cat /usr/share/asterisk/build-portability.txt
section 'CPU visible inside the VM container'
collect docker exec "$CONTAINER" sh -c 'uname -m; grep -m1 -E "^(model name|Processor|Hardware)[[:space:]]*:" /proc/cpuinfo; grep -m1 -E "^(flags|Features)[[:space:]]*:" /proc/cpuinfo'
for CLI in \
  'core show version' \
  'core show settings' \
  'core show uptime' \
  'core show channels concise' \
  'pjsip show channelstats' \
  'module show like codec_g722.so' \
  'module show like res_rtp_asterisk.so' \
  'module show like bridge_native_rtp.so' \
  'module show like bridge_simple.so' \
  'core show translation'; do
  section "$CLI"
  collect docker exec "$CONTAINER" asterisk -rx "$CLI"
done
section 'Recent container logs: may contain phone numbers/IPs or previously enabled SIP traces'
collect docker logs --timestamps --since "$SINCE" --tail 250 "$CONTAINER" 2>&1
section 'Interpretation (not an automatic root-cause verdict)'
cat <<'EOF'
An increased RestartCount proves restarts, not their cause. Use the die event:
  132 = SIGILL: investigate builder-only CPU instructions / BUILD_NATIVE.
  139 = SIGSEGV; 134 = SIGABRT: obtain a matching native backtrace.
  137 = SIGKILL: correlate an oom event/kernel log before calling it an OOM.
  0   = orderly process exit/restart: correlate config-sync/re-exec logs.
A native_rtp/simple_bridge switch is a normal media-bridge event, not proof of
SIP REFER transfer or of a crash. No die event may mean history expired; an
in-place restart also need not create a Docker die event. Run the collector
immediately after reproduction and record UTC start/end times. Asterisk core
dumps and raw SIP logs can contain secrets; do not post them unredacted.
EOF
