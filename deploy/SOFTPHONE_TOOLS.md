# Softphone host-tools bundle (Windows build → image-only VM)

`docker save`/`docker load` transfers images, NOT the repository files under
`/opt/Calling_integration`. An image import cannot install a script on the host
or configure a separate 1Panel/OpenResty container automatically.

This small bundle provides the host-side proxy installer/checker, deployment
verification scripts, an offline recognizer self-test, nginx snippet, licence
notice, and guides:

- `scripts/finish-deployment.py` (one-command post-image-load setup and checks)
- `scripts/install-softphone-proxy.py`
- `scripts/check-softphone-websocket.py`
- `scripts/check-voicemail-transcription.py`
- `scripts/verify-release.sh` (legacy CRLF-safe verification wrapper)
- `scripts/diagnose-extension-audio.sh`
- `deploy/nginx/softphone-websocket.conf`
- `deploy/RELEASE_CHECKLIST.md` (complete Windows → VM and check-everything guide)
- `deploy/SOFTPHONE_TOOLS.md` (this guide)
- `deploy/THIRD_PARTY_NOTICES.md`
- `deploy/fixtures/README.md` and `deploy/fixtures/goforward-8k.wav`

It contains no `.env`, passwords, databases, Compose file or application images.
You do NOT need to rebuild images, build on the VM, or pull Git on the VM to
install this bundle into an existing deployment. Start with
`deploy/RELEASE_CHECKLIST.md` for the exact image/file transfer commands and
post-deployment checks.

## Quick path: one command after `docker image load`

After extracting this archive into `/opt/Calling_integration`, run in a
maintenance window:

```bash
cd /opt/Calling_integration
python3 scripts/finish-deployment.py latest tel.engineerip.com
```

The script starts/recreates services, refreshes host tools from the loaded
image, waits for Asterisk, installs/validates the `/ws` route, verifies offline
transcription, public WebSocket, fresh admin UI and Compose state. It removes
the temporary `docker create` helper container automatically. No server build,
Git pull, or manual `tools_container` command is needed. The rest of this file
is the manual fallback and troubleshooting reference.

## A. Repair the existing deployment with the small archive

Copy `softphone-proxy-tools.tar.gz` to `/tmp` on the VM using WinSCP/scp, as you do
with the image archive. If you have a checksum, copy it too and verify it in the
same directory with `sha256sum -c softphone-proxy-tools.tar.gz.sha256`.

On the VM:

```bash
cd /opt/Calling_integration
# Back up any installer/checker/snippet you edited locally before extracting.
# This archive contains only the allowlisted host-tools files listed above.
tar -xzf /tmp/softphone-proxy-tools.tar.gz -C /opt/Calling_integration
ls -l scripts/install-softphone-proxy.py
sudo python3 scripts/install-softphone-proxy.py --domain tel.engineerip.com
```

After reviewing that dry-run's selected proxy/vhost/upstream:

```bash
sudo python3 scripts/install-softphone-proxy.py --domain tel.engineerip.com \
  --apply --connect-network
```

Replace the hostname for another site. If multiple nginx/OpenResty containers
are detected, add `--proxy-container NAME` from `docker ps`. Use `sudo tar` only
if the target checkout is not writable by its owner; do not change `.env` or
repository ownership to work around a permission failure.

The installer checks Asterisk directly, backs up the persistent vhost, validates
nginx syntax, reloads ONLY the web proxy, then tests the public SIP WebSocket
upgrade. Only a valid HTTP 101 with subprotocol `sip` is `PASS`. Direct-preflight
failures make no config/network changes. Syntax/reload failures restore the
original vhost. A valid installed route with a failed public check is reported
`NOT VERIFIED`; keep the output for diagnosis rather than assuming it worked.

The installer does not restart Asterisk, register a phone, place calls, change
outbound routing or request credentials. Passing the WebSocket upgrade is not
proof of SIP registration or two-way audio.

## B. Future images also contain this bundle

New API images built with the updated Dockerfile include `/app/deploy-kit`:
the host installer/checkers, release checklist, offline verification script and
sample, audio incident collector, nginx snippet, and licence notice.
After importing a NEW image built with that Dockerfile, extract the tools from a
**stopped temporary container**. Replace `YOUR_TAG` with the tag you loaded:

```bash
cd /opt/Calling_integration
tools_container=$(docker create engineerip/telephony-api:YOUR_TAG)
docker cp "${tools_container}:/app/deploy-kit/." /opt/Calling_integration/
docker rm "$tools_container"
```

Stop if `docker create` or `docker cp` fails. `docker create` does not start the
application or publish ports. The existing API/PBX containers keep running.
Use the exact imported tag, not an older running API container. If
`/app/deploy-kit` is missing in that image, use option A: it was built before this
packaging correction. Do not rebuild on the low-spec VM.

The application image deliberately has NO Docker socket or privilege to modify
host nginx. Host extraction/apply is explicit; launching/loading an image alone
does not install or enable the proxy route.

## C. Building/transferring on Windows

Keep your existing Docker Desktop build → save `.tar` → transfer → load workflow.
Update the Windows source/Dockerfile before your next local build so the API
image includes `/app/deploy-kit`. No server-side build is required.
`deploy/RELEASE_CHECKLIST.md` has copy/paste PowerShell, `scp`, checksum,
`docker load`, and verification commands for the complete release.

For example, in **PowerShell on Windows** (with Docker Desktop using Linux
containers), build and save both images locally. Use a fresh tag:

```powershell
$tag = "softphone-v2"
docker build --platform linux/amd64 -t "engineerip/telephony-api:${tag}" .
docker build --platform linux/amd64 -t "engineerip/asterisk:${tag}" ./asterisk
docker image save --output "engineerip-telephony-${tag}.tar" "engineerip/telephony-api:${tag}" "engineerip/asterisk:${tag}"
```

Use `--output` rather than piping binary image data through PowerShell. Match the
platform to the VM (`linux/amd64` for x86-64). These commands are for the build
workstation, NOT the low-spec VM. For a planned release, import the `.tar` on the
VM with `docker load --input FILE.tar` and use the same `IMAGE_TAG` when starting
Compose. Replacing Asterisk can interrupt calls, so do this in a maintenance
window. None of that is required just to apply option A to today's deployment.

With Python installed locally (PowerShell), create the small host-tools archive
without building images:

```powershell
py -3 scripts/package-softphone-tools.py --output softphone-proxy-tools.tar.gz
```

The Bash/WSL `build-export-images.sh` also produces an image archive AND a small
`engineerip-softphone-tools-TAG.tar.gz` sidecar/checksum, extracting the tools from
the built image without requiring workstation Python. Copy that sidecar to the VM
and use its actual filename in option A.

## Persist a bridged 1Panel proxy's private network

A host-networked proxy uses host loopback and needs no extra network. A bridged
proxy uses Docker DNS `engineerip-asterisk:8088`. If the installer attaches that
proxy to `engineerip-telephony-internal`, persist it as an external network in
1Panel's proxy Compose configuration, preserving all existing networks. For example
(use the actual proxy service name):

```yaml
services:
  openresty:
    networks:
      # Keep this service's existing networks too.
      - engineerip-telephony-internal
networks:
  engineerip-telephony-internal:
    external: true
    name: engineerip-telephony-internal
```

Do not add `networks` to a proxy using `network_mode: host`. A live network
attachment survives restarts but not replacement/recreation, and a panel may
regenerate the vhost after UI edits. Re-check `/ws` after those operations. The
installer does not rewrite the panel's Compose file or expose Asterisk's ARI
HTTP server publicly.
