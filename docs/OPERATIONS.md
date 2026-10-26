# Build, Transfer, Deploy, and Operate

This guide is for building the CPU-intensive Asterisk image on a personal computer and running the completed images on a smaller Google Compute Engine VM.

## Image-only / Windows users: why a host installer can be missing

`docker save` exports **images only**. `docker load` does not update
`/opt/Calling_integration/scripts` on the VM, even if the running container has the
latest application. A missing `install-softphone-proxy.py` on the host is not a
reason to build on the low-spec server or to restart a working PBX.

For the current missing-installer problem, copy/extract the small host-tools
archive as described in [`deploy/SOFTPHONE_TOOLS.md`](../deploy/SOFTPHONE_TOOLS.md).
It contains only the installer, its standard-library checker, the nginx snippet
and instructions. It does not overwrite `.env`, Compose, databases or images.
Run the dry-run, then the reviewed apply step on the VM. No server Git checkout
update is required for this archive path.

New API images additionally embed `/app/deploy-kit`, so any Windows Docker
Desktop build → save `.tar` → transfer → load workflow can carry the installer.
You can copy that directory out of a **stopped temporary container** using the
exact imported image tag; see the same guide. The old images built before this
Dockerfile correction do not have that directory: use the standalone tools
archive rather than repeatedly trying to copy a nonexistent path.

The kit is not auto-executed by the API; the application has no Docker socket or
host-proxy privileges. Loading/starting images alone cannot configure 1Panel.

## 1. Requirements

Personal computer:

- Docker Desktop or Docker Engine with Compose v2
- at least 4 GB free RAM during the Asterisk compilation
- enough disk space for Docker layers and the exported archive

VM:

- Linux x86-64 (the scripts default to `linux/amd64`)
- Docker Engine and Compose v2
- a static public IP
- UDP 5060 and UDP 10000-10100 allowed only as needed

If the VM is ARM64, build with `PLATFORM=linux/arm64`. The archive architecture must match the VM (`uname -m`).

## 2. Configure without putting secrets in the image

Secrets are not baked into either image. On the VM:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Set every `GENERATE...`/`CHANGE-ME` value. `ASTERISK_EXTERNAL_ADDRESS` is the first-boot fallback for the public SIP/RTP address; after setup the admin panel's **Service address** is the single source of truth - and when it changes, the configuration sync writes the transports and restarts Asterisk by itself (`pjsip reload` never rebinds a transport), so no manual restart is needed or helpful. The restart waits for calls to clear (`core restart when convenient`) - it happens at once on an idle PBX and never tears down a live call. Confirm it landed with `docker compose logs asterisk` and `pjsip show transport transport-wss | grep -i external` (the new numeric address, without touching the container). Keep the same `SECRET_KEY` after first deployment because it encrypts SIP and webhook secrets in the settings database.

Generate secrets, for example:

```bash
openssl rand -hex 32
```

## 3. Build and export on the personal computer

From the repository root:

```bash
chmod +x scripts/*.sh
./scripts/build-export-images.sh v1
```

This builds tagged `linux/amd64` images and creates:

- `engineerip-telephony-v1.tar.gz`
- `engineerip-telephony-v1.tar.gz.sha256`
- `engineerip-softphone-tools-v1.tar.gz` (**copy this too**)
- `engineerip-softphone-tools-v1.tar.gz.sha256`

The Bash exporter copies the host kit from the locally built API image using a
stopped temporary container. It does not start the application. If building and
saving images manually in Windows PowerShell, you can keep that workflow: the new
API Dockerfile embeds the same kit. Alternatively package the small tools without
building any image: `py -3 scripts/package-softphone-tools.py`.

For an ARM64 VM:

```bash
PLATFORM=linux/arm64 ./scripts/build-export-images.sh v1
```

## 4. Copy to Google VM

For an existing image-only deployment, keep its `.env`/Compose configuration and
copy the image archive/checksum plus the small host-tools archive/checksum. Extract
the tools separately as described above. No Git pull or image build is needed on
the VM. For an initial deployment, also supply `docker-compose.yml` and prepare the
VM's private `.env` separately. Copying the full source tree is optional. Example
for operators who do choose to copy the source:

```bash
gcloud compute scp --recurse . VM_NAME:~/Calling_integration --zone=ZONE
```

Do not copy a workstation `.env` through an unsafe channel and never commit it.

## 5. Import and start without rebuilding

On the VM:

```bash
cd ~/Calling_integration
chmod +x scripts/*.sh
./scripts/import-start-images.sh engineerip-telephony-v1.tar.gz v1
```

The script verifies the checksum, loads both images, creates `crm-network` if necessary, and runs `docker compose up -d --no-build`. It accepts both Windows `.tar`
and compressed `.tar.gz` image exports. `--no-build` is important on a low-resource
VM. Import/start is a planned release operation and may recreate Asterisk, dropping
active calls; use a maintenance window. **Do not import/restart containers just to
install the missing proxy helper**: the small tools archive repair needs neither.
The import helper does not silently edit nginx or overwrite host configuration.

Verify:

```bash
docker compose ps
docker compose logs --tail=100 asterisk telephony-ari telephony-api
curl http://127.0.0.1:5000/health
```

A 503 during the first few seconds is normal while ARI connects. It should become HTTP 200.

Then gate the release on the three things a browser call cannot work without -
each of these has silently regressed at least once:

```bash
# 1. the softphone's SIP stack ships in the API image and is served
curl -s http://127.0.0.1:5000/jssip.min.js | head -c 40      # JsSIP v3.10.1 banner

# 2. the outbound guard compares inside $[...] (the ] belongs after the comparison)
docker exec engineerip-asterisk sh -lc \
  "grep GotoIf /etc/asterisk/dynamic/extensions.dynamic.conf | head -1"

# 3. DTLS-SRTP is compiled in, or every WebRTC call fails at media setup
docker exec engineerip-asterisk asterisk -rx "module show like res_srtp"

# 4. the softphone's WebSocket path reaches Asterisk - this is what
#    "SIP WebSocket disconnected." means when it is broken (the softphone
#    dials wss://<the host serving the phone page>/ws - probe YOUR_WEB_HOST)
docker exec engineerip-asterisk asterisk -rx "http show status"
docker exec engineerip-asterisk asterisk -rx "module show like res_http_websocket"
docker exec engineerip-asterisk asterisk -rx "module show like res_pjsip_transport_websocket"
python3 scripts/check-softphone-websocket.py ws://127.0.0.1:8088/ws
python3 scripts/check-softphone-websocket.py wss://YOUR_WEB_HOST/ws
# Both must PASS a real 101 handshake selecting subprotocol sip. A plain
# curl GET/HEAD does not test the upgrade. Direct PASS + public FAIL isolates
# the public proxy path; direct 404 means investigate Asterisk's URI/modules.
# Install/include deploy/nginx/softphone-websocket.conf in the active HTTPS
# vhost as described in DEPLOYMENT.md; rebuilding the app cannot configure nginx.

# 5. the media path - a call that connects and stays completely silent is
#    almost always RTP going to (or coming from) an address nobody can reach
docker exec engineerip-asterisk asterisk -rx "pjsip show transport transport-wss" | grep -i external
#   ExternalMediaAddress must be a NUMERIC public IP (the renderer resolves the
#   Service address); the host firewall must also pass the RTP range (default
#   UDP 10000-10100) from the internet, not just 5060 and 443
dig +short YOUR_SIP_HOST | head -2                    # must equal the VM's public IP
#   call audio travels DIRECTLY to whatever the Service address resolves to:
#   a CDN/proxy record (e.g. a proxied Cloudflare record) hands out an edge IP
#   that never carries RTP, and a name that resolves to nothing now stops the
#   config sync with a self-explaining error instead of shipping a name into
#   SDP. The console's Server-addresses page shows the same error when it fires.

# 6. the browser softphone can find a media path: without a STUN server a
#    phone behind a router only offers its private address and every call
#    rings in silence. Blank "Browser media (ICE servers)" on the console's
#    Server-addresses page means the built-in public STUN; a network that
#    needs TURN must supply its own there - the console hands that list to
#    every softphone it opens, and a test call from an external network is
#    the only real proof of two-way audio

# 7. browser and phone identities are separate - a Zoiper matched to the
#    WebRTC endpoint gets DTLS-SRTP it cannot speak: connected, billed, silent
docker exec engineerip-asterisk sh -lc \
  "grep -A2 'type=endpoint' /etc/asterisk/dynamic/pjsip.dynamic.conf | grep -B2 webrtc=yes | head -4"
#   the webrtc=yes endpoint must be named <SIP username>-webrtc, and the plain
#   <SIP username> must sit on the plain endpoint beside it
```

Transport changes now restart Asterisk automatically when the rendered
transports change - `pjsip reload` never rebinds them - so a Service address
set after first boot reaches the running system without a manual restart. The
same automatic restart fires whenever a generated endpoint disappears (switching
WebRTC off, deleting an extension), because a removed section can linger in
Asterisk's memory after a plain reload and keep answering for a device that must
never reach it again.

After importing a release, reload each open phone page once. The phone's service
worker deletes every cache of an earlier release when the new one activates; a
page opened before that can still show the previous release.

## 6. Admin console

The API/admin port binds to VM loopback only. Use an HTTPS reverse proxy, a VPN, or an SSH tunnel:

```bash
ssh -L 5000:127.0.0.1:5000 USER@VM_PUBLIC_IP
```

For a production configuration (`SESSION_COOKIE_SECURE` enabled), put HTTPS in front of the service. Then open `/admin`, sign in with `ADMIN_USERNAME` and the initial `ADMIN_PASSWORD`, and change the password.

The responsive console uses a sidebar with dedicated Dashboard, Extensions, Phone Numbers, SIP Providers, Call History, Recordings, Webhooks, Call Settings, and Security pages. It supports:

- extension creation, update, search, disabling, and safe deletion;
- any number of DID/phone-number records, each owned by one extension for matching outbound caller ID and callback routing;
- SIP provider creation, update and safe deletion, including source-IP allowlists;
- searchable, paginated call history across all extensions;
- recordings grouped and filtered by extension, with authenticated in-browser playback;
- extension voicemail enablement/PINs plus mailbox-grouped playback, read, urgent, and delete management;
- SendGrid voicemail attachments configured only by the main administrator;
- additional admin-panel users with extension-scoped calls, recordings and voicemail;
- per-customer default outbound and inbound fallback extension, set by the customer on their Numbers page;
- per-extension recording: each device opts in on its own switch, with no global policy;
- multiple CRM webhook endpoints, event filters, bearer tokens, and delivery tests;
- dashboard health, call totals, answer rate, resource counts and recent activity.

Changes to extensions, DIDs, and providers render atomic Asterisk include files and request PJSIP/dialplan reload through private AMI.

## 7. Recording storage and playback

Recordings persist in the `asterisk_recordings` Docker volume. They are not mounted into the public API container. Playback uses an authenticated API/admin request which streams the file through private ARI. This preserves the Asterisk/API security boundary.

Back up the volume before destructive maintenance. Do not run `docker compose down -v` unless all telephony settings, call metadata, and recordings may be deleted.

```bash
docker volume ls | grep engineerip
```

Retention is set in Admin > Call recording. The ARI worker removes expired recordings based on the persisted call end time.

## 8. Upgrades and rollback

Build each release under a new immutable tag (`v2`, a date, or a commit SHA). Import it and start it with that same tag. To roll back, set/export the previous `IMAGE_TAG` and run:

```bash
IMAGE_TAG=v1 docker compose up -d --no-build
```

Image changes do not erase named volumes. Always back up before schema or production upgrades.

## 9. Troubleshooting

### Image tries to build on the VM

Use `docker compose up -d --no-build` and make sure `IMAGE_TAG` exactly matches `docker image ls`.

### `exec format error`

The image architecture does not match the VM. Rebuild with `PLATFORM=linux/amd64` or `linux/arm64` as appropriate.

### Asterisk container is unhealthy

```bash
docker compose logs asterisk
docker compose exec asterisk asterisk -rx 'core show version'
docker compose exec asterisk asterisk -rx 'pjsip show registrations'
docker compose exec asterisk asterisk -rx 'pjsip show endpoints'
```

Check all required `.env` values. Provider allowlists must contain real provider IP/CIDR ranges, not example addresses.

### Calls connect but have no audio

Confirm the admin panel's Service address (or the `ASTERISK_EXTERNAL_ADDRESS` fallback), UDP 10000-10100 firewall rules, VM NAT rules, and provider codecs. Do not expose ARI 8088 or AMI 5038.

### Admin change is saved but not active

Inspect API logs for AMI reload failures and ensure both API and Asterisk mount `asterisk_dynamic_config` with group ID 2000 as defined by the images.

### Recording does not play

Only finalized recordings can play. Check the call's recording status, ARI logs, browser network response, and free space in the recording volume.

### Webhook test fails

Verify DNS/routing from `telephony-api`, TLS certificates, the URL path, and whether the CRM accepts the configured bearer token. A test payload uses event `webhook.test`.

## 10. Backups

At minimum back up these named volumes:

- `telephony_data` (settings and call metadata)
- `asterisk_recordings`
- `asterisk_voicemail` (extension mailbox messages)
- `asterisk_lib`, `asterisk_spool`, and `asterisk_keys` as required by policy

Also retain the exact compose file, `.env` in a secure secret store, and imported image tag used for the deployment.

## Log growth and full disks

Docker output is capped per container and Asterisk file logs are held in tmpfs. Existing containers must be recreated before new logging options apply. For diagnosis and safe recovery, use `scripts/cleanup-logs.sh` and follow [`LOGGING_AND_DISK.md`](LOGGING_AND_DISK.md). Never use `docker compose down -v` or `docker system prune --volumes` to solve a log problem.

## Recording controls and VPS load

Recording is disabled by default at two levels: the administrator master policy and each extension preference. A bridge is recorded only when both are enabled. Administrators manage the master switch under **Call settings** and extension preferences under **Extensions**. Assigned users can opt their own extension in or out under **Security > My call recording**, but cannot override a disabled master switch. Keep the master switch off when minimizing CPU, disk I/O, and storage is more important than recordings.

## Verify voicemail, the browser socket, and recording finalisation after an upgrade

Rebuild/import the updated `telephony-api` image and recreate **both**
`telephony-api` and `ari-worker` (they use that image). Keep all named volumes.
The worker syncs the saved configuration on startup. Saving a Main Number or
Call Handling flow now also renders/reloads the PBX immediately; a saved-but-not-
applied warning must be resolved before the flow is considered live.

1. **Browser phone:** close/reopen or reload the phone after the upgrade so the
   updated service worker activates. Its shell is now network-first, with an
   offline fallback, rather than indefinitely serving an older `phone.js`.
   A legacy socket equal to `wss://<SIP-domain>/ws` migrates once to
   `wss://<page-host>/ws`; the SIP domain itself is unchanged. Other custom socket
   addresses, and explicit choices saved after migration, are preserved. In the
   browser's Network / WS tab, confirm `/ws` on the intended HTTPS host returns
   **101 Switching Protocols**, then confirm SIP registration succeeds. The HTTP
   diagnostic fetch can be blocked by CORS: its failure alone does not establish
   a DNS or certificate failure.
2. **Voicemail:** save an enabled mailbox in the relevant flow. Call the DID,
   let the desks ring out, listen through the greeting, leave more than two
   seconds of speech (`minsecs=2`), and hang up. Confirm the message appears and
   plays in the correct number's mailbox. Repeat with multiple ringing devices:
   their late teardown events must not cut off the caller's greeting/message.
   Dial `*97` to check mailbox access and greeting recording separately.
3. **Recording:** enable the platform and answering extension's recording
   switches, complete an answered **ARI-managed bridge** call, then check the
   recording becomes `finalized` and plays/downloads. A saved file whose finish
   event was missed is reconciled by the worker every 30 seconds. An ARI outage
   or missing stored response is not taken as proof that audio was lost. Failed
   attempts stay visible rather than disappearing. Recovery can expose an
   existing file; it cannot reconstruct audio that was never written.

The direct registered-phone outbound `Dial()` route is unchanged by this fix;
bridge-recording tests do not prove recording coverage for that separate route.
When reporting a missing recording, include the call ID (if present), timestamp
with timezone, direction and whether it was dialled directly in a SIP/browser
phone or initiated from the console/API. Do not include SIP passwords or tokens.

For a failed live acceptance call, correlate that ID/time with:

```bash
docker compose logs --since=15m telephony-api ari-worker asterisk
docker compose exec asterisk asterisk -rx 'voicemail show users for engineerip'
docker compose exec asterisk asterisk -rx 'dialplan show voicemail-inbound'
```

A voicemail continuation failure and recording setup failure now log the call
ID in the worker; an AMI apply failure is logged by the API and shown in the
flow-save warning. These checks are live acceptance gates, not something unit
tests alone can certify.

### Shared incoming numbers in the routing studio

Both roles now use **Number flow** and **Extension handling**. To share a desk,
open Extension handling, select the desk, and tick its primary number and the
company number under **Numbers that ring this desk**, then save. This edits the
numbers' direct ring stages; it does not reassign the desk or change outbound
calling. Existing IVR menus still run before the direct ring stage—map their
keys separately if callers should choose that desk from the menu.

After deploying, call both DIDs externally and confirm the same registered desk
rings. Test ring-device off → voicemail, and ring-out → forwarding separately.
When several desks ring, configure a number-level fallback for a deterministic
shared ending; without one the first selected desk supplies the no-answer action.
A general number can use a saved journey without provisioning a duplicate desk.


## 1Panel/OpenResty softphone `/ws` installation

For a repeated `/ws` 404 with 1Panel/container-managed HTTPS, use the
container-aware installer rather than copying the **host** nginx upstream into a
bridged proxy. Run on the VM, first without `--apply` to see the selected vhost,
network and upstream:

```bash
sudo python3 scripts/install-softphone-proxy.py --domain tel.engineerip.com
sudo python3 scripts/install-softphone-proxy.py --domain tel.engineerip.com \
  --apply --connect-network
```

Add `--proxy-container NAME` when auto-discovery finds more than one proxy. Review
DEPLOYMENT.md's 1Panel section before applying. In bridge mode, persist the shared
external network in the panel's proxy Compose settings so a future recreation
keeps connectivity. Host-networked OpenResty needs no extra network. A `PASS`
means the real public SIP WebSocket upgrade succeeded, not SIP registration/audio.
If the direct Asterisk preflight fails, no config/network is changed. If nginx
rejects the patch, the installer restores the original vhost. Valid routes with
failed public verification are explicitly `NOT VERIFIED`, not silently rolled
back or called fixed. This operation does not restart Asterisk or alter outbound
calling.

## Extension-to-extension audio loss and Asterisk restarts

A CLI returning to the Linux shell plus an increased container `RestartCount`
requires investigating Asterisk's process exit **before** diagnosing RTP/NAT.
`Running=true ExitCode=0` after Docker has restarted the container does not
identify the preceding exit. Nor does a `native_rtp`/`simple_bridge` technology
switch prove a SIP transfer or a crash; a bridge technology change is normal.

Immediately after reproducing 101 → 105, run on the VM:

```bash
bash scripts/diagnose-extension-audio.sh > extension-audio.log 2>&1
```

This is a finite, read-only snapshot: container state plus historical Docker
`die`/`oom`/restart events, image portability metadata, VM CPU flags, channel/media
statistics, codec/bridge modules, and bounded existing logs. It does **not** enable
SIP/RTP logging, reload or restart anything, change a codec, or place a call. Review
and redact IPs, phone numbers and previously enabled SIP traces before sharing the
log. Never share `.env`, complete Docker environments or raw core dumps.

Docker keeps a bounded event history. Capture it promptly and record the exact UTC
call time; a missing event is not proof that no crash occurred. An in-place Asterisk
restart also need not create a Docker `die` event. Interpret recorded exit values:

- **132 / SIGILL:** inspect `BUILD_NATIVE` and CPU capabilities. The image is built
  on a workstation then exported to the VM. Upstream Asterisk enables
  `BUILD_NATIVE` by default, adding `-march=native` to compilation, including
  bundled pjproject. A codec/media function can execute instructions present on
  the builder but missing on the VM. Successful G.711 carrier calls do not prove
  all G.722/internal media paths are safe.
- **139 / SIGSEGV or 134 / SIGABRT:** obtain a native backtrace against the exact
  image/binaries before assigning a module/root cause. A portability fix does not
  prove these faults repaired. Core dumps may contain SIP and provider secrets;
  collect/store them privately. Frame pointers are retained in new images.
- **137 / SIGKILL:** correlate Docker `oom` events and the host kernel log; do not
  label every SIGKILL as an OOM. The running container's OOM flag may no longer
  describe the prior event.
- **0:** inspect configuration-sync/re-exec/deployment logs around the event; an
  orderly exit is not evidence of a native crash.

### Portable image correction

`asterisk/Dockerfile` now explicitly disables `BUILD_NATIVE` before the parallel
build, retains `KEEP_FRAME_POINTERS`, and fails the build if the native flag remains.
The resulting image records `/usr/share/asterisk/build-portability.txt`. Merely
pulling source, changing the dialplan, restarting an old container or importing the
old image cannot fix machine instructions compiled into existing binaries.

Rebuild/export a **new tag** on the build machine using the documented build/export
script; do not reuse an old tar archive. Import it using `import-start-images.sh`
in a maintenance window: replacing the Asterisk container interrupts active calls.
Confirm the image actually running on the VM:

```bash
docker exec engineerip-asterisk cat /usr/share/asterisk/build-portability.txt
docker exec engineerip-asterisk asterisk -rx 'core show settings'
```

The metadata must report `BUILD_NATIVE=disabled`, and Asterisk's build options
must not include `BUILD_NATIVE`. The compile policy changes no SIP usernames,
trunks, caller ID, codec order, outbound dial strings or RTP/media settings. Do
not force G.711 globally or disable bridging just to hide an unclassified crash.

### Required live acceptance (not covered by the Python suite)

1. Record Asterisk's current image ID, uptime and Docker `RestartCount`.
2. With two phones on the same numbered line, call 101 → 105, then 105 → 101.
   Verify both directions of speech for at least 60 seconds, and repeat calls.
3. Repeat with the devices' normal G.722/G.711 negotiations and, if used, a browser
   and a plain SIP device. Preserve the separate RTP/DTLS identities. Capture
   `pjsip show channelstats` while connected; a connected call with zero RTP still
   needs media-address/NAT investigation if the process stayed alive.
4. Confirm uptime continues, restart count does not increase, no new fatal event
   occurs, both sides hear audio, and hangup clears both channels.
5. Make one existing external-call smoke test without altering its working
   credentials, provider codecs or routing.

No Docker/Asterisk/SIP phones are available in the coding sandbox, so build-policy
and dialplan tests cannot certify those live media/crash checks. The user's live
exit event/backtrace and this acceptance run are required before declaring the
reported incident resolved.
