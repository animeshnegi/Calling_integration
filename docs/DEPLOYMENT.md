# Deployment Guide

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

Set strong random values for `SECRET_KEY`, `TELEPHONY_TOKEN`, `CRM_WEBHOOK_TOKEN`, `ASTERISK_ARI_PASSWORD`, `ASTERISK_AMI_PASSWORD`, and every extension password.

Set these IPComms values from the IPComms portal:

```text
IPCOMMS_SIP_SERVER=<your-tenant>.s1.ipcomms.net
IPCOMMS_SIP_PORT=5060
IPCOMMS_SIP_USERNAME=<trunk username>
IPCOMMS_SIP_PASSWORD=<trunk password>
IPCOMMS_DID=+13022661626
IPCOMMS_ALLOWED_IPS=<provider IP 1>,<provider IP 2>
```

`IPCOMMS_ALLOWED_IPS` is required. The Asterisk entrypoint renders each provider IP/CIDR as a separate PJSIP `identify` match.

Set `ASTERISK_EXTERNAL_ADDRESS` to the VPS public IP or public telephony hostname used for SIP/RTP NAT.

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

The Asterisk entrypoint validates required environment variables, creates required runtime directories, generates the bootstrap PJSIP transports/extensions/provider objects when the dynamic database configuration is empty, and then starts Asterisk in the foreground. Docker's healthcheck verifies that Asterisk is running and that `transport-udp` is loaded before dependent services start.

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

Do **not** put the IPComms trunk credentials into Zoiper. Zoiper should register to Asterisk extension `101`:

```text
Username: 101
Password: EXTENSION_101_PASSWORD
Server: <VPS public IP or telephony hostname>
Port: 5060
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
