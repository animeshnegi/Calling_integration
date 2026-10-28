# One-time Windows → VM release and check-everything checklist

This checklist is for the current deployment shape:

- Windows workstation builds Docker images.
- Linux VM does **not** build, clone Git, download AI models, or run `docker compose build`.
- VM checkout: `/opt/Calling_integration`.
- SSH destination in the examples: `engineerip7@ipseekers`.
- Public admin/softphone domain: `tel.engineerip.com`.
- HTTPS proxy: 1Panel / OpenResty in Docker.
- Existing `.env`, `SECRET_KEY`, database, and voicemail volumes stay on the VM.

`docker image save` moves **images only**. It does not create or update
`/opt/Calling_integration/scripts/*.py`, `docker-compose.yml`, nginx snippets,
or 1Panel's vhost. Those host files must be copied separately. This is why
`python3 scripts/install-softphone-proxy.py` was previously missing on the VM.

Never paste an API key, `.env`, database, or full `nginx -T` dump into chat.
Replace example hostnames/tags if your environment differs.

---

## 0. Simple path: two commands after uploading the archives

Copy the image `.tar` (+ `.sha256`) and `softphone-proxy-tools.tar.gz`
(+ `.sha256`) to `/tmp` on the VM. Then run **in a maintenance window**:

```bash
cd /opt/Calling_integration
tar -xzf /tmp/softphone-proxy-tools.tar.gz -C .
python3 scripts/finish-deployment.py \
  --image-tar /tmp/engineerip-telephony-20261027-local1.tar \
  20261027-local1 tel.engineerip.com
```

If you already ran `docker image load`, omit `--image-tar`. For the `latest`
tag and the standard domain, the second command is simply:

```bash
python3 scripts/finish-deployment.py
```

An already loaded image can be verified with the new small tools archive; no
full image rebuild is needed just to fix these host-side steps. If verification
reports `engine_unavailable` or a missing `no-store` header, rebuild the API
image from the current source on Windows.

That one script:

1. verifies/loads the image archive and checks its architecture;
2. starts/recreates all Compose services with `--no-build`—so
   `engineerip-asterisk` exists before the proxy installer runs;
3. uses the corrected host tools from the extracted archive and normalizes
   Windows CRLF line endings—no `tools_container`/`docker cp` step;
4. waits for Asterisk's direct SIP WebSocket;
5. dry-runs, applies and validates the exact `/ws` route;
6. runs the offline transcription self-test with `--network none`, bind-mounting
   the corrected host script/sample into the loaded image;
7. checks the public `wss` upgrade, `admin.js` no-store header, and Compose
   state.

Add `--proxy-container NAME` if more than one nginx/OpenResty container exists.
Use `--skip-proxy` only if the `/ws` route is already managed another way.
`bash scripts/verify-release.sh` remains a CRLF-safe wrapper around
`finish-deployment.py --verify-only`; it now defaults to `latest` and
`tel.engineerip.com`.

Sections 1–5 below explain each file and provide the manual fallback. **Do not
run their individual start/install/verify commands separately if the
one-command script succeeded.**

## 1. What belongs where

| Item | Windows workstation | VM `/opt/Calling_integration` | Docker image | Docker volume |
|---|---|---|---|---|
| Application code and admin UI | source checkout used only to build | not required | **yes** | no |
| PocketSphinx + English model | installed by `docker build` | not installed on host | **yes** | no |
| `.env` / `SECRET_KEY` | do not copy from VM | **keep existing** | no | no |
| `docker-compose.yml` | copy only if changed | **one current copy** | no | no |
| `scripts/install-softphone-proxy.py` | source / tools archive | **copy from archive** | also in `/app/deploy-kit` | no |
| `scripts/check-softphone-websocket.py`, `diagnose-extension-audio.sh`, `verify-release.sh` | source / tools archive | **copy from archive** | also in `/app/deploy-kit` | no |
| nginx snippet and release docs | source / tools archive | **copy from archive** | also in `/app/deploy-kit` | no |
| settings/calls DB and voicemail audio | never transfer | no | no | **keep existing** |

The current host-tools archive contains no `.env`, passwords, Compose file,
database, or application image. Images and host files are both required.

---

## 2. Windows: build and export

In **PowerShell**, from the updated repository root. Docker Desktop must use
Linux containers. Use a new tag every release; the example uses
`20261027-local1`.

```powershell
$Tag = "20261027-local1"
$ImageTar = "engineerip-telephony-${Tag}.tar"
$ToolsTar = "softphone-proxy-tools.tar.gz"

docker build --platform linux/amd64 -t "engineerip/telephony-api:$Tag" .
docker build --platform linux/amd64 -t "engineerip/asterisk:$Tag" ./asterisk
docker image save --output $ImageTar "engineerip/telephony-api:$Tag" "engineerip/asterisk:$Tag"

# Linux-compatible sha256sum -c format, not PowerShell's default hash-only output.
$ImageHash = (Get-FileHash $ImageTar -Algorithm SHA256).Hash.ToLower()
"$ImageHash  $ImageTar" | Set-Content -Encoding ascii "${ImageTar}.sha256"

# Creates the small host-tools archive and its checksum.
py -3 scripts/package-softphone-tools.py --output $ToolsTar
```

If `py` is unavailable, use `python` or `python3`. If OpenSSH `scp` is
unavailable, use WinSCP to copy the same four resulting files in the next step.

Optional: use `scripts/build-export-images.sh` under Bash/WSL instead; it
produces both an image archive and a tools sidecar with checksums.

### Upload with `scp`

```powershell
scp $ImageTar "${ImageTar}.sha256" $ToolsTar "${ToolsTar}.sha256" engineerip7@ipseekers:/tmp/
```

Or drag those four files into `/tmp` with WinSCP. They are:

- `engineerip-telephony-20261027-local1.tar`
- `engineerip-telephony-20261027-local1.tar.sha256`
- `softphone-proxy-tools.tar.gz`
- `softphone-proxy-tools.tar.gz.sha256`

Only copy `docker-compose.yml` if you actually changed it. Back up and compare
the VM's existing file first; do not overwrite local panel-specific edits.

```powershell
scp .\docker-compose.yml engineerip7@ipseekers:/tmp/docker-compose.new.yml
```

Never upload or overwrite `.env`. Keep the server's existing `SECRET_KEY`, or
saved email/SIP credentials will become unreadable.

---

## 3. VM: verify, load image, place host files (manual fallback)

Skip this section if you used the one-command script in section 0. Use the same
tag on both machines:

```bash
TAG=20261027-local1
cd /tmp
sha256sum -c "engineerip-telephony-$TAG.tar.sha256"
sha256sum -c softphone-proxy-tools.tar.gz.sha256

cd /opt/Calling_integration
cp -a docker-compose.yml "docker-compose.yml.bak.$TAG"   # only if replacing Compose
# Compare before replacing; keep the existing file if the only changes are local.
# cp /tmp/docker-compose.new.yml docker-compose.yml

docker image load --input "/tmp/engineerip-telephony-$TAG.tar"

# Inspect before extracting. Expected paths start with scripts/ and deploy/.
tar -tzf /tmp/softphone-proxy-tools.tar.gz
tar -xzf /tmp/softphone-proxy-tools.tar.gz -C /opt/Calling_integration
ls -l scripts/install-softphone-proxy.py scripts/verify-release.sh \
      scripts/check-voicemail-transcription.py deploy/RELEASE_CHECKLIST.md
```

Use `sudo tar` only if your user cannot write the existing checkout. Do not
change ownership of `.env`, the database, or voicemail volumes.

If Compose reads `IMAGE_TAG` from `.env`, update just that line so a later
`docker compose up` keeps using the loaded image:

```bash
sed -i.bak.$TAG "s/^IMAGE_TAG=.*/IMAGE_TAG=$TAG/" .env
grep '^IMAGE_TAG=' .env
```

Skip that edit if your `.env` does not contain `IMAGE_TAG`; passing the variable
in the next command is enough. Do not print the rest of `.env`.

---

## 4. Install the softphone `/ws` route (manual fallback)

A repeated `https://tel.engineerip.com/ws` **HTTP 404** means the active HTTPS
vhost does not forward that exact path to Asterisk. Loading a new Docker image
cannot fix it: 1Panel's proxy configuration is a host-side file.

Preview first:

```bash
cd /opt/Calling_integration
sudo python3 scripts/install-softphone-proxy.py --domain tel.engineerip.com
```

Review the selected proxy container, persistent vhost, upstream, and network.
Then apply:

```bash
sudo python3 scripts/install-softphone-proxy.py --domain tel.engineerip.com \
  --apply --connect-network
```

If more than one nginx/OpenResty proxy is running, add
`--proxy-container NAME` from `docker ps`. For a bridged proxy, persist the
attached external network in 1Panel's proxy Compose configuration, preserving
all existing networks. A host-networked proxy uses host loopback and needs no
new network.

The installer requires a valid SIP WebSocket upgrade directly from Asterisk
before touching files. It backs up the vhost, checks `nginx -t`, reloads only
that proxy, and reports `NOT VERIFIED` if the public check fails. A passing
check is HTTP **101** with the `sip` subprotocol:

```bash
python3 scripts/check-softphone-websocket.py wss://tel.engineerip.com/ws
```

This proves the browser's WebSocket transport, not SIP registration, RTP, or
two-way call audio.

---

## 5. Recreate application services from the loaded image (manual fallback)

There is no VM build:

```bash
cd /opt/Calling_integration
TAG=20261027-local1
IMAGE_TAG="$TAG" docker compose up -d --no-build --force-recreate \
  telephony-api telephony-ari
docker compose ps telephony-api telephony-ari
```

Both services use the **same** rebuilt `engineerip/telephony-api` image: the
API serves the admin UI; `telephony-ari` runs the voicemail email worker and the
offline recognizer. Recreating these two does not recreate Asterisk and does
not change outbound calling. Replacing the Asterisk image is a separate
maintenance-window decision.

Run the bundled read-only verification:

```bash
python3 scripts/finish-deployment.py --verify-only "$TAG" tel.engineerip.com
# Legacy equivalent: bash scripts/verify-release.sh
```

It checks:

1. the loaded image architecture matches the VM;
2. a real 8 kHz WAV is transcribed **with `--network none`**;
3. public `/ws` returns a valid SIP WebSocket 101;
4. `admin.js` has `Cache-Control: no-store`;
5. Compose service state.

It is Python-based so a Windows CRLF copy cannot break `set -o pipefail`.

It does **not** authenticate to an email provider, prove inbox delivery, register a
phone, or prove internal-call audio. Those checks follow.

---

## 6. Email sending

After the new API image is loaded, recreate **both** the API and ARI worker;
for this email-only update no Asterisk image, host-tools archive, proxy change or
server build is required:

```bash
cd /opt/Calling_integration
docker compose up -d --no-build --no-deps --force-recreate telephony-api telephony-ari
```

Use the same image tag as your existing Compose setup. Keep `.env`, `SECRET_KEY`,
MySQL/SQLite data and voicemail volumes. The provider column is added on startup;
existing SendGrid settings stay on SendGrid.

Hard-refresh the admin page (`Ctrl+F5`), open **Email Delivery**, select
**Elastic Email**, enter its API key with **Send HTTP** permission and a sender
verified in that account, and click **Save & send test email**. Re-enter the
Elastic key when switching from the old SendGrid-only form: a saved credential is
never silently forwarded to a different provider. Blank preserves a key for the
same provider. Turn on voicemail email and set the extension's notification
address for automatic delivery.

The provider test uses the same Elastic Email v2 POST form fields as the working
example; voicemail audio is sent as a multipart attachment. Even HTTP 200 can
contain `success: false`, which is reported as a failure with a safe hint. A
provider-accepted test is not proof of inbox delivery; check provider activity
and spam. The test does not test transcription. There is **no transcription API key**.

For HTTP 401, check the selected provider's API key and send permission. For
HTTP 403, check the sender/domain verification, permissions and account/IP
restrictions. For failed voicemail sends, inspect **Recent email deliveries**.

Failed deliveries retry after 60 seconds up to five attempts. Changing the
provider, key or sender, or re-enabling email resets failed attempts without
re-sending accepted mail. The voicemail inbox retains the original audio.

---

## 7. Voicemail transcription workflow

Transcription is entirely local. PocketSphinx and its English model are inside
the API image. No OpenAI key, Whisper API, speech-provider upload, usage fee,
runtime model download, or GPU is used. An email provider is still required to send
the resulting text and original audio.

Correct sequence:

1. Enable **Enable voicemail email**.
2. Enable **Include a voicemail transcript**.
3. Save; set the extension's notification email and leave voicemail enabled.
4. Leave a **new, unread, non-sensitive test voicemail**.
5. Wait at least 10 seconds for the file to settle, then up to about 30–60
   seconds for the ARI worker's next scan.
6. Open the email: it should contain escaped plain/HTML transcript text **and
   the original WAV attachment**.
7. Check **Recent email deliveries → Transcript**.

The **Save & send test email** button has no recording and deliberately does
not test transcription. To test the recognizer without a voicemail:

```bash
cd /opt/Calling_integration
TAG=20261027-local1
docker run --rm --network none -w /app \
  -v "$PWD/scripts/check-voicemail-transcription.py:/tmp/check.py:ro" \
  -v "$PWD/deploy/fixtures/goforward-8k.wav:/tmp/goforward-8k.wav:ro" \
  "engineerip/telephony-api:$TAG" python /tmp/check.py \
  --sample /tmp/goforward-8k.wav
# Expected: "status": "transcribed", "go forward ten meters"
```

Bind-mounting the corrected host script and sample also verifies an older
loaded image whose `/app/deploy-kit` still has the former wrong fixture path.

Status meanings:

| Status | Meaning / action |
|---|---|
| `transcribed` | Text was generated locally; original audio is still attached. |
| `empty transcript` | No recognisable speech; audio-only note is included. |
| `unsupported audio` / `audio too long` | Only mono 16-bit PCM WAV at 8/16 kHz, up to 5 minutes / 20 MiB, is supported. Legacy GSM emails without text. |
| `engine unavailable` | Loaded image lacks PocketSphinx; rebuild/load the current image. |
| `resource limit` / `timeout` / `worker error` | A bounded decoder worker failed; the audio email still sends. Check RAM/CPU and logs. |
| `disabled` | The transcript switch is off. |

Transcripts are machine-generated English, may be inaccurate, and are not
stored in the database. No language selector or external model is included.

---

## 8. Internal softphone audio is a separate check

The `/ws` installer fixes a 404/registration transport problem. It does **not**
prove that internal `101 ↔ 105` calls keep two-way audio. The earlier live
symptom was brief audio, silence, and a CLI shell/restart; external calls were
working and outbound routing remains unchanged.

After the release, perform one plain 101↔105 call in a maintenance window.
If audio drops, collect a read-only incident snapshot **immediately** and note
UTC start/end times:

```bash
cd /opt/Calling_integration
bash scripts/diagnose-extension-audio.sh > "/tmp/extension-audio-$(date -u +%Y%m%dT%H%M%SZ).log" 2>&1
```

Review that file locally; it can include phone numbers/IPs. Look for the Docker
`die` event's `exitCode`, `RestartCount`, `OOMKilled`, image ID, CPU portability
marker, `pjsip show channelstats`, bridge/codec modules, and Asterisk logs.
Exit 132 suggests a builder-only CPU instruction (`BUILD_NATIVE`); 139/134
requires a native backtrace; 137 needs OOM/kernel correlation; 0 needs
re-exec/config-sync log correlation. A portable build fixed one concrete
shipping defect but did **not** prove the cause of the unclassified live
restart. Do not change outbound calling during this investigation.

---

## 9. If something still fails

Send only the exact inline error wording and the relevant sanitized line from:

- `python3 scripts/finish-deployment.py --verify-only "$TAG" tel.engineerip.com`
  (or its legacy `bash scripts/verify-release.sh` wrapper);
- `python3 scripts/check-softphone-websocket.py wss://tel.engineerip.com/ws`;
- **Recent email deliveries** status/error;
- Email provider activity result (`Delivered`, `Blocked`, `Bounce`, `Dropped`);
- `/tmp/extension-audio-*.log` for internal audio, with phone numbers/IPs
  redacted.

Do **not** send the email API key, `.env`, database, raw `nginx -T`, raw core
dump, or unredacted SIP traces. This repository's automated tests cover the
application logic and a real offline WAV decode; live provider authentication,
inbox delivery, 1Panel state, MySQL behavior, and Asterisk media still require
these checks on your VM.
