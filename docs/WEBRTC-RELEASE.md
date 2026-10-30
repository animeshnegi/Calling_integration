# Calling audio and voicemail delivery: image-only release

This release shortens normal browser ICE setup, adds ringing and ringback,
provides music and a visible held-party message in the browser, removes the
speaker selector (Windows selects the output), and fixes associated-account
voicemail email and shared mailbox permissions. It does **not** change outbound
number routing or the existing SIP/1Panel proxy topology. See
[WEBRTC.md](WEBRTC.md) for the behavior and ICE limitations.

**Update the Asterisk image and the API image together.** The Asterisk image
contains MOH configuration/audio and repairs old voicemail volume permissions;
the API image supplies the loop, updated browser, voicemail scanner and offline
recognizer. Browser endpoints now pass hold signaling through instead of asking
the PBX to generate music. A browser still running an old tab would not play
the loop or show the remote hold notice. Close and reopen all softphone windows
after deployment. Plan a maintenance window: recreating Asterisk ends active
calls. The ARI service uses the API image and must also be recreated.

## 1. Build images on Windows, then copy the archive

Run from an updated checkout on the Arena branch, with Docker Desktop available.
Choose a new tag each time rather than overwriting a running release:

```powershell
git fetch origin
git checkout arena/24f0d1a3-calling-integration
git pull --ff-only origin arena/24f0d1a3-calling-integration
$Tag = "calling-voicemail-20261029"
docker build --platform linux/amd64 -t "engineerip/telephony-api:$Tag" .
if ($LASTEXITCODE -ne 0) { throw "API image build failed" }
docker build --platform linux/amd64 -t "engineerip/asterisk:$Tag" ./asterisk
if ($LASTEXITCODE -ne 0) { throw "Asterisk image build failed" }
docker save -o "calling-$Tag.tar" "engineerip/telephony-api:$Tag" "engineerip/asterisk:$Tag"
if ($LASTEXITCODE -ne 0) { throw "Image export failed" }
(Get-FileHash "calling-$Tag.tar" -Algorithm SHA256).Hash
scp "calling-$Tag.tar" engineerip7@ipseekers:/tmp/
if ($LASTEXITCODE -ne 0) { throw "Copy failed" }
```

The `docker build` commands do not need the VM's `.env` or credentials on
Windows. `linux/amd64` assumes an x86-64 VM; use the VM's actual architecture
if different. Use the VM's reachable SSH hostname/IP if `ipseekers` is not
resolvable from Windows. The VM does not need Git, source changes, a compiler
or an image build.

## 2. Load and recreate on the VM

Connect as `engineerip7`. Compare `sha256sum` with the Windows hash before
loading, and inspect the tagged images. Preserve `.env`, `SECRET_KEY`, database,
voicemail and all Docker volumes; do **not** run `docker compose down -v`.

```bash
set -eu
TAG=calling-voicemail-20261029
sha256sum "/tmp/calling-$TAG.tar"
docker load -i "/tmp/calling-$TAG.tar"
docker image inspect "engineerip/telephony-api:$TAG" "engineerip/asterisk:$TAG" >/dev/null
cd /opt/Calling_integration
IMAGE_TAG="$TAG" docker compose up -d --no-build --no-deps --force-recreate asterisk telephony-ari telephony-api
docker compose ps
```

The command deliberately uses `--no-build`, so it cannot compile on the VM. Once
all services are healthy, record `IMAGE_TAG=calling-voicemail-20261029` in the
VM's existing `.env` for future Compose operations; keep the other settings and
secrets unchanged. If its Compose file uses an explicit image tag instead of
`${IMAGE_TAG}`, update that tag to the imported images before recreating the
services. Do not depend on a new deployment script existing on the VM: the
API image includes its own deploy kit if proxy assistance is needed.

Verify the loaded PBX can see the packaged music and module:

```bash
docker exec engineerip-asterisk asterisk -rx 'module show like res_musiconhold'
docker exec engineerip-asterisk asterisk -rx 'module show like format_wav'
docker exec engineerip-asterisk asterisk -rx 'moh show classes'
```

The `default` class should point at `/usr/share/asterisk/engineerip-moh`. If the site uses
other container names, use `docker compose exec -T asterisk ...` instead.
Reopen the admin console and softphone windows; accept the browser microphone
prompt and enable incoming sound when the phone asks. If a phone was opened from
the console, leave the console open for its blocked-popup ringing fallback.

## 3. Verify voicemail email and transcription

Docker loading an image does **not** update the host's Compose file. The
current Compose pins the API and ARI worker to the same shared volume/context;
an older file may not pin the worker. Check only the non-secret path values:

```bash
cd /opt/Calling_integration
IMAGE_TAG="$TAG" docker compose exec -T telephony-ari python -c \
  'from app.config import Config; print(Config.VOICEMAIL_PATH, Config.VOICEMAIL_CONTEXT)'
```

It should print `/app/voicemail engineerip`. If it does not, correct only
`VOICEMAIL_PATH=/app/voicemail` and `VOICEMAIL_CONTEXT=engineerip` in the VM's
existing `.env`, preserving everything else, then recreate `telephony-ari` with
`IMAGE_TAG="$TAG" docker compose up -d --no-build --no-deps --force-recreate telephony-ari`.
The existing Compose must still mount the same voicemail volume at
`/app/voicemail` in the API and ARI services and at
`/var/spool/asterisk/voicemail` in Asterisk. Do not replace volumes.

The image contains an offline speech self-test; no host script or network is
needed. Run it using the same loaded tag (it prints only a public sample):

```bash
docker run --rm --network none "engineerip/telephony-api:$TAG" \
  python /app/deploy-kit/scripts/check-voicemail-transcription.py
```

Then hard-refresh the admin console, open **Email Delivery**, and:

1. Keep the saved Elastic Email key and verified sender; turn on **Enable
   voicemail email**. **Save & send test email** checks provider acceptance,
   but does not prove inbox delivery or test audio/transcription.
2. Confirm the customer's account email. Under **Extensions**, leave the
   optional override blank to use that account, set a custom address, or choose
   **Do not send voicemail email**. An enabled voicemail flow step also counts.
   Automatic account email applies to **new** recordings after this upgrade;
   older messages remain in the console and are not sent in a backlog.
3. If text is wanted, turn on **Include a voicemail transcript**. Leave a new,
   non-sensitive spoken English voicemail longer than two seconds. Allow 10
   seconds for the recording to settle and roughly 30–60 seconds for a scan.
4. Check the intended inbox and spam, **Recent email deliveries** and Elastic
   Email activity. The received email should include the original audio, an
   optional machine-generated transcript, and the automated/do-not-reply
   notice. If recognition fails, the audio email is still sent. Provider
   acceptance does not prove delivery. A live inbox test is required.

See [EMAIL_AND_USERS.md](EMAIL_AND_USERS.md) for ownership, opt-out and retry
behavior. Do not paste credentials, private voicemail or full `.env` values into
support messages.

## 4. Test on the actual phones and network

1. Place an extension call between two browsers. The caller should hear
   ringback, and the destination PC should ring before answer. Verify two-way
   conversation, hold in each direction, the held browser's message and music,
   then resume and verify two-way conversation again.
2. Repeat between a browser and a plain SIP phone in both directions. A held
   plain SIP phone should hear the PBX's music; whether it displays a hold badge
   depends on the third-party app. When the browser is held, it should show its
   notice and play music.
3. Call an external number without changing the routing and confirm ringback or
   the carrier's early announcement, connection and two-way audio. Incoming PC
   ringing should stop on answer, decline, cancellation and logout.
4. If sound is missing, check the selected Windows default output, its volume,
   and browser site-sound permissions. The browser may require a click on
   **Enable incoming call sound**; a separate phone window without a console
   opener has no console fallback. The test environment cannot verify a real
   PC speaker, carrier, public NAT or the user's TURN service.

The browser's quick ICE release is candidate-aware, but it cannot guarantee
that a very slow STUN/TURN server or restrictive network produced a reachable
candidate. If media fails in the live network, capture the actual ICE candidates
and RTP path as described in [WEBRTC.md](WEBRTC.md), rather than treating a
successful image load or a provisional SIP response as proof of audio.
