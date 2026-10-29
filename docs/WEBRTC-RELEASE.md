# Browser phone signalling / ICE release

This release addresses a reproduced failure in Asterisk's WebSocket INVITEs to
browser extensions: JsSIP rejects a numeric-leading Docker container hostname
when it appears as a single-label SIP host in From or Contact. The **API image**
now renders a valid `from_domain` on browser endpoints only and the phone shell
is version 12. It also keeps the prior public ICE candidate fix. Carrier and
plain-phone endpoint settings are not changed.

The Asterisk image must include the earlier ICE fix (`cb32c14` or later). Build
and export **both** images under one new tag so the existing VM Compose file can
use that tag for all services later. If the VM is already running the ICE-fixed
Asterisk image, only API and ARI need recreation for this release. Use a quiet
maintenance window: recreating the app/ARI worker can interrupt app-managed
call handling and event tracking. The worker reloads PJSIP, but may also schedule
an Asterisk restart if an earlier restart is pending or other configuration has
changed. Recreating Asterisk disconnects active calls.

## Windows PowerShell: build and export

Use an updated checkout containing this release and Docker Desktop in
Linux-container mode. Confirm the commit printed by `git log -1 --oneline` is
this release (or a descendant) before building. Never use VM secrets to build.
The tag must be new: reusing an existing tag can make an old image look current.

```powershell
$ErrorActionPreference = "Stop"
$Tag = "webrtc-ring-v12-20261029"
$ImageTar = "engineerip-telephony-$Tag.tar"
git log -1 --oneline
docker build --platform linux/amd64 -t "engineerip/telephony-api:$Tag" .
if ($LASTEXITCODE -ne 0) { throw "API image build failed" }
docker build --platform linux/amd64 -t "engineerip/asterisk:$Tag" ./asterisk
if ($LASTEXITCODE -ne 0) { throw "Asterisk image build failed" }
docker image save --output $ImageTar "engineerip/telephony-api:$Tag" "engineerip/asterisk:$Tag"
if ($LASTEXITCODE -ne 0) { throw "Image export failed" }
$ImageHash = (Get-FileHash $ImageTar -Algorithm SHA256).Hash.ToLower()
"$ImageHash  $ImageTar" | Set-Content -Encoding ascii "${ImageTar}.sha256"
scp $ImageTar "${ImageTar}.sha256" engineerip7@ipseekers:/tmp/
if ($LASTEXITCODE -ne 0) { throw "Image transfer failed" }
```

Or copy both files to `/tmp` with WinSCP. No build or Git checkout is needed on
the VM. Keep the existing `.env`, `SECRET_KEY`, database and voicemail volumes;
never run `docker compose down -v` as part of this update.

## VM: import and start without building

If the current Asterisk already advertises its public ICE candidate, leave it
running and recreate the two API-image services only:

```bash
(
  set -eu
  TAG=webrtc-ring-v12-20261029
  cd /tmp
  sha256sum -c "engineerip-telephony-$TAG.tar.sha256"
  docker image load --input "engineerip-telephony-$TAG.tar"
  cd /opt/Calling_integration
  IMAGE_TAG="$TAG" docker compose up -d --no-build --pull never --no-deps --force-recreate telephony-ari telephony-api
  docker compose ps asterisk telephony-ari telephony-api
)
```

The temporary `IMAGE_TAG` applies to that command only. After backing up `.env`
with its permissions preserved, privately set its `IMAGE_TAG` to the same new
tag for future Compose commands, without printing or changing the other values.
A later `docker compose up` that includes Asterisk can recreate it to use the
new tag, so schedule that for a quiet window too.
If the running Asterisk is **not** already on the ICE-fixed image, recreate it
with that new tag in a maintenance window instead; this drops active calls:

```bash
cd /opt/Calling_integration
IMAGE_TAG=webrtc-ring-v12-20261029 docker compose up -d --no-build --pull never --force-recreate asterisk telephony-ari telephony-api
```

## Verify

Close and reopen the phone window from the console to load the version 12 shell.
For the relevant browser username (not its password), inspect only its live
endpoint's `from_domain`, for example:

```bash
docker exec engineerip-asterisk asterisk -rx \
  'pjsip show endpoint EXAMPLE_102_13025550001-webrtc' | grep -i from_domain
```

It should be the configured SIP Service hostname or a valid numeric public
address, not a Docker container ID. The actual address must be verified with a
fresh INVITE: JsSIP should send `100 Trying` and `180 Ringing`. Answer and check
two-way audio, then test browser-to-phone in the other direction. Public ICE
candidates do not prove UDP reachability; firewalls, port-preserving NAT and
TURN can still matter. SIP logs contain telephone numbers, digest
`Authorization` headers and ICE credentials; do not share them unredacted.
Disable temporary tracing after testing:

```bash
docker exec engineerip-asterisk asterisk -rx 'pjsip set logger off'
docker exec engineerip-asterisk asterisk -rx 'rtp set debug off'
```

Local regressions include actual Asterisk 22.11 with Chromium and a plain SIP
client on loopback: the original invalid From/Contact was reproduced, the
rendered fix produced 100/180/200 and ACK/BYE, and both call directions
exchanged decoded audio. An additional simulated test exercises a digest
challenge. These tests do not cover the deployed VM, public NAT, carrier or an
actual Zoiper installation.
