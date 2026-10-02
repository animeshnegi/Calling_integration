# Deployment

> Production requires `DATABASE_URI` pointing to a reachable MySQL database. The application creates missing tables automatically. Complete [MYSQL.md](MYSQL.md) before starting the containers. Guide

For building the images on a personal computer, exporting them, and importing them on a low-resource Google VM without rebuilding, follow [`OPERATIONS.md`](OPERATIONS.md) first. This guide covers telephony configuration and live verification.

## 1. Create/connect the private Docker network

If EngineerIP already creates a Docker network named `crm-network`, use it. Otherwise create it once:

```bash
docker network create crm-network
```

The telephony API joins that network, allowing the CRM container to reach it by its Docker DNS name.

## 2. Configure secrets and IPComms

```bash
cp .env.example .env
nano .env
```

Set strong random values for `SECRET_KEY`, `TELEPHONY_TOKEN`, `ASTERISK_ARI_PASSWORD`, `ASTERISK_AMI_PASSWORD`, and `ADMIN_PASSWORD`.

That is the whole file. Telephony itself is configured in the console, not in the
environment: sign in as the administrator, add the carrier under **Carrier
Providers**, then assign a number to a customer. The platform creates the extension,
its SIP credentials and the default call flows, so a new line works without a
redeploy.

`ASTERISK_EXTERNAL_ADDRESS` must be the VPS public IP or public telephony hostname
used for SIP/RTP NAT, and `ASTERISK_RTP_START`/`ASTERISK_RTP_END` must match the UDP
range opened in the firewall (default `10000-10100`).

The seed values `ASTERISK_EXTENSIONS`, `DEFAULT_EXTENSION`, `EXTENSION_<number>_PASSWORD`
and the `IPCOMMS_*` block are optional and commented out in `.env.example`. They exist
only for deployments upgrading from a version that kept telephony settings in the
environment: set before first start, they pre-fill the database once, and the console
owns them afterwards. New installs can leave them out entirely.

**Never commit `.env` or provider credentials.**

## 3. RTP and firewall

The default RTP range is UDP `10000-10100`. Keep these aligned:

- `ASTERISK_RTP_START=10000`
- `ASTERISK_RTP_END=10100`
- Docker's published RTP range
- VPS firewall UDP `10000:10100`

The VPS firewall must be updated manually when the range changes.

## 4. Build/start

```bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=200 asterisk
```

Static Asterisk configuration is part of the image. The dynamic PJSIP and dialplan files are stored in the shared `asterisk_dynamic_config` volume so the API/ARI worker can update Asterisk without rebuilding the image. `/etc/asterisk` itself is intentionally not a Docker volume, so a newly imported image cannot be masked by stale configuration.

The Asterisk entrypoint validates its runtime/ARI/AMI variables, creates required directories, and generates only the static PJSIP transports before starting Asterisk in the foreground. The ARI worker then bootstraps the administration database from optional environment credentials and renders extensions, providers, registrations, identify rules, and DID routing into the dynamic includes. Keeping SIP objects out of the static bootstrap file prevents duplicate PJSIP object errors after an admin reload. Docker's healthcheck verifies that Asterisk and `transport-udp` are ready before the worker synchronizes configuration.

## 5. Verify Asterisk

```bash
docker compose exec asterisk asterisk -rx 'core show version'
docker compose exec asterisk asterisk -rx 'pjsip show transports'
docker compose exec asterisk asterisk -rx 'pjsip show endpoints'
docker compose exec asterisk asterisk -rx 'pjsip show registrations'
docker compose exec asterisk asterisk -rx 'pjsip show contacts'
docker compose exec asterisk asterisk -rx 'http show status'
```

The first carrier milestone is:

```text
IPComms registration: Registered
```

If registration is rejected, check the SIP server, username, password, UDP 5060 access, source IP allow-list, and IPComms account status before testing a phone.

## 6. Test Zoiper through Asterisk

Do **not** put the IPComms trunk credentials into Zoiper. Register the phone to the
extension the console provisioned. Open the customer in `/admin`, or the customer
opens **Devices & SIP**, and use **Credentials** on the extension:

```text
Username: 101                     (the extension number - fixed by the platform)
Password: shown once by Credentials in the console
Server:   <VPS public IP or telephony hostname>
Port:     5060
Transport: UDP
```

Then verify:

```bash
docker compose exec asterisk asterisk -rx 'pjsip show contacts'
```

The extension should appear as reachable/available.

From Zoiper, dial a full E.164 number such as `+1...`. The dialplan sends the call through the configured provider endpoint.

## 7. Verify inbound DID

After carrier registration is confirmed, call the IPComms DID from an external phone.

Expected flow:

```text
PSTN -> IPComms -> Asterisk -> from-provider -> configured inbound extension -> Zoiper/IP phone
```

Watch the Asterisk console if troubleshooting:

```bash
docker compose logs -f asterisk
```

## 8. Verify Flask and ARI worker

```bash
docker compose logs --tail=100 telephony-ari telephony-api
curl http://127.0.0.1:5000/health
```

The API health endpoint should return HTTP 200 only when Asterisk is reachable and the ARI worker readiness file is fresh.

## 9. Connect EngineerIP CRM

Attach EngineerIP's CRM container to `crm-network` if it is not already attached:

```bash
docker network connect crm-network engineerip-crm
```

Then set the CRM integration base URL to:

```text
http://engineerip-telephony-api:5000
```

Use `POST /api/v1/calls` from server-side CRM code. Keep `TELEPHONY_TOKEN` server-side.

## 10. Outbound click-to-call verification

The intended production sequence is:

```text
CRM server
   -> POST /api/v1/calls
   -> persist call_id + employee channel ID
   -> Asterisk rings employee extension
   -> employee answers
   -> persist customer channel ID
   -> Asterisk originates customer through provider
   -> customer answers
   -> mixing bridge joins employee + customer
   -> optional bridge recording
   -> hangup
   -> recording finalized
   -> bridge destroyed
   -> call completed + CRM webhook
```

Test the complete sequence with a controlled destination and inspect both the API/ARI worker logs and Asterisk CLI output.

## 11. Restart/recovery verification

During an active test call:

```bash
docker compose restart telephony-ari
```

The worker reconciles persisted incomplete calls with channels that are still live in Asterisk. Verify that the call does not become orphaned and that the correct customer/bridge lifecycle continues.

## 12. WebRTC / TLS

Asterisk's HTTPS/WSS listener is on TCP 8089. The startup script creates a temporary self-signed certificate if none exists, which is useful for container bring-up but **is not a production browser certificate**.

Before browser WebRTC production use, mount a trusted certificate whose hostname matches the browser WSS hostname, or terminate TLS at the reverse proxy and configure the topology consistently.

Do **not** publish Asterisk ARI TCP 8088 to the Internet. ARI is reachable only from the private telephony Docker network.

## 13. VPS firewall

For the current IPComms UDP test, allow only what is required:

- UDP 5060 for SIP
- UDP 10000-10100 for RTP
- TCP 8089 only when using Asterisk WSS directly or as required by the reverse-proxy design
- TCP 5000 should normally remain private and not be publicly exposed

Do not open TCP 8088 for ARI or TCP 5038 for AMI to the Internet.

## 14. Production hardening

- Use strong unique SIP credentials per employee.
- Disable anonymous SIP.
- Restrict inbound provider identification to IPComms source addresses.
- Apply outbound destination permissions and call-duration limits.
- Use TLS/SRTP for browser/device traffic where appropriate.
- Back up Asterisk configuration and call/recording storage.
- Monitor disk usage if recordings are enabled.
- Rate-limit public API endpoints at the reverse proxy.
- Keep `.env` and certificates outside Git.
- Run dependency/container vulnerability scans before production release.
- Test outbound-call authorization and rate limits against the real CRM identity model before enabling click-to-call for multiple users.

## Testing boundary

Automated tests verify the Python application and call-state transitions with fake Asterisk clients. They cannot prove live carrier registration, inbound DID delivery, NAT/audio, Zoiper registration, WebRTC microphone access, RTP quality or IPComms behavior. Those require the deployed VPS and live provider account.
