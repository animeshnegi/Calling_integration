# Build, Transfer, Deploy, and Operate

This guide is for building the CPU-intensive Asterisk image on a personal computer and running the completed images on a smaller Google Compute Engine VM.

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

Set every `GENERATE...`/`CHANGE-ME` value. `ASTERISK_EXTERNAL_ADDRESS` must be the VM public IP or telephony hostname. Keep the same `SECRET_KEY` after first deployment because it encrypts SIP and webhook secrets in the settings database.

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

For an ARM64 VM:

```bash
PLATFORM=linux/arm64 ./scripts/build-export-images.sh v1
```

## 4. Copy to Google VM

Copy the repository (without `.git` if desired), image archive, checksum, and the VM's separately prepared `.env`. Example:

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

The script verifies the checksum, loads both images, creates `crm-network` if necessary, and runs `docker compose up -d --no-build`. `--no-build` is important on a low-resource VM.

Verify:

```bash
docker compose ps
docker compose logs --tail=100 asterisk telephony-ari telephony-api
curl http://127.0.0.1:5000/health
```

A 503 during the first few seconds is normal while ARI connects. It should become HTTP 200.

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
- default outbound and inbound fallback extension selection;
- global and per-extension recording policy;
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

Confirm the public IP in `ASTERISK_EXTERNAL_ADDRESS`, UDP 10000-10100 firewall rules, VM NAT rules, and provider codecs. Do not expose ARI 8088 or AMI 5038.

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
