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

Set strong random values for `SECRET_KEY`, `TELEPHONY_TOKEN`, `CRM_WEBHOOK_TOKEN`, and `ASTERISK_ARI_PASSWORD`.

Set these IPComms values from the IPComms portal:

```text
IPCOMMS_SIP_SERVER=<your-tenant>.s1.ipcomms.net
IPCOMMS_SIP_PORT=5060
IPCOMMS_SIP_USERNAME=<trunk username>
IPCOMMS_SIP_PASSWORD=<trunk password>
IPCOMMS_DID=+13022661626
IPCOMMS_ALLOWED_IPS=<provider IP 1>,<provider IP 2>
```

Set `ASTERISK_EXTERNAL_ADDRESS` to the VPS public IP or the public telephony hostname that resolves to the VPS.

Set a strong `EXTENSION_101_PASSWORD` for Zoiper/SIP phones. `WEBRTC_EXTENSION_PASSWORD` may be separate.

**Never commit `.env` or provider credentials.** `.gitignore` already excludes `.env` and Asterisk keys.

## 3. RTP and firewall

The default RTP range is UDP `10000-10100`. Keep all three values aligned:

- `ASTERISK_RTP_START=10000`
- `ASTERISK_RTP_END=10100`
- Docker's published `10000-10100/udp` range
- VPS firewall UDP `10000:10100`

If IPComms gives a different RTP range, change the environment and Docker publish range together before deployment.

## 4. Build/start

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=150 asterisk
```

The Asterisk container generates `pjsip.conf` at startup from the `.env` values. Real SIP credentials therefore never enter GitHub.

## 5. Verify Asterisk

```bash
docker compose exec asterisk asterisk -rx 'core show version'
docker compose exec asterisk asterisk -rx 'pjsip show endpoints'
docker compose exec asterisk asterisk -rx 'pjsip show registrations'
docker compose exec asterisk asterisk -rx 'http show status'
```

The first carrier milestone is:

```text
IPComms registration: Registered
```

If registration is rejected, check the SIP server, username, password, UDP 5060 access, and IPComms account status before testing a phone.

## 6. Test Zoiper through Asterisk

Do **not** put the IPComms trunk credentials into Zoiper for this test. Zoiper should register to Asterisk extension `101`:

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

From Zoiper, dial a full E.164 number such as `+1...`. The dialplan sends the call through the `ipcomms` endpoint.

## 7. Verify Flask

```bash
curl http://127.0.0.1:5000/health
```

It will return 503 until ARI is reachable and authenticated.

## 8. Connect EngineerIP CRM

Attach EngineerIP's CRM container to `crm-network` if it is not already attached:

```bash
docker network connect crm-network engineerip-crm
```

Then set the CRM integration base URL to:

```text
http://engineerip-telephony-api:5000
```

Use `POST /api/v1/calls` from server-side CRM code. Keep `TELEPHONY_TOKEN` server-side.

## 9. WebRTC / TLS

Asterisk's HTTPS/WSS listener is on TCP 8089. The startup script creates a temporary self-signed certificate if none exists, which is useful for container bring-up but **is not a production browser certificate**.

Before browser WebRTC production use, mount a trusted certificate whose hostname matches the browser WSS hostname, or terminate TLS at the reverse proxy and configure the topology consistently.

Do **not** publish Asterisk ARI TCP 8088 to the Internet.

## 10. VPS firewall

For the current IPComms UDP test, allow only what is required:

- UDP 5060 for SIP
- UDP 10000-10100 for RTP
- TCP 8089 only when using Asterisk WSS directly or as required by the reverse-proxy design
- TCP 5000 should normally remain private and not be publicly exposed

Do not open TCP 8088 for ARI to the Internet.

## 11. Production hardening

- Use strong unique SIP credentials per employee.
- Disable anonymous SIP.
- Restrict inbound provider identification to IPComms source addresses.
- Apply outbound destination permissions and call-duration limits.
- Use TLS/SRTP for browser/device traffic where appropriate.
- Back up Asterisk configuration and call/recording storage.
- Monitor disk usage if recordings are enabled.
- Rate-limit public API endpoints at the reverse proxy.
- Keep `.env` and certificates outside Git.

## Testing boundary

The GitHub repository can be reviewed and syntax-tested, but real carrier registration, inbound DID delivery, NAT/audio, Zoiper registration, WebRTC and call quality require the deployed VPS and live IPComms account. Those are verified only after the corresponding VPS tests succeed.
