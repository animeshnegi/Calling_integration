# Deployment Guide

## 1. Create/connect the private Docker network

If EngineerIP already creates a Docker network named `crm-network`, use it. Otherwise create it once:

```bash
docker network create crm-network
```

The telephony API joins that network, allowing the CRM container to reach it by its Docker service/container DNS name.

## 2. Configure secrets

```bash
cp .env.example .env
```

Set strong random values for `SECRET_KEY`, `TELEPHONY_TOKEN`, `CRM_WEBHOOK_TOKEN`, and the Asterisk ARI password. Set your SIP provider values only in `.env` or a secret manager.

Never commit `.env`.

## 3. TLS certificate for WebRTC

A trusted certificate is required for normal browser WebRTC operation. Mount a certificate/key into `/etc/asterisk/keys/asterisk.pem`, or terminate TLS at the reverse proxy and configure the Asterisk/browser topology consistently.

The certificate hostname should match the WSS hostname used by the browser.

## 4. Build/start

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 asterisk
docker compose logs --tail=100 telephony-api
```

## 5. Verify Asterisk from inside the network

```bash
docker compose exec asterisk asterisk -rx 'core show version'
docker compose exec asterisk asterisk -rx 'pjsip show endpoints'
docker compose exec asterisk asterisk -rx 'http show status'
```

## 6. Verify Flask

```bash
curl http://127.0.0.1:5000/health
```

It will return 503 until ARI is actually reachable and authenticated.

## 7. Connect EngineerIP CRM

Attach EngineerIP's CRM container to `crm-network` if it is not already attached:

```bash
docker network connect crm-network engineerip-crm
```

Then set the CRM integration base URL to the telephony API container name, e.g.:

```text
http://engineerip-telephony-api:5000
```

Use the `POST /api/v1/calls` endpoint from server-side CRM code. The master `TELEPHONY_TOKEN` must remain server-side.

## 8. Reverse proxy / WebRTC

Publish only the browser-facing HTTPS/WSS endpoint through your normal reverse proxy. Do not publish Asterisk ARI 8088. If using a separate hostname such as `telephony.example.com`, proxy WebSocket upgrades to Asterisk's HTTPS/WSS listener and keep ARI on the private network.

## 9. VPS firewall

Allow only the ports you actually need. Typical public telephony ports are:

- UDP/TCP 5060 for SIP where the provider requires it.
- UDP 10000-10100 for RTP.
- TCP 8089 only when exposing Asterisk's WebRTC HTTPS/WSS directly through your reverse proxy design.

Do **not** open TCP 8088 for ARI to the Internet.

Providers may use different SIP transports/ports. Follow the provider's firewall requirements.

## 10. Production hardening

- Use strong unique SIP credentials per employee.
- Disable or restrict anonymous SIP.
- Apply outbound dialing permissions and destination controls.
- Set call duration limits to reduce toll fraud risk.
- Use TLS/SRTP/VPN appropriate to your environment.
- Back up Asterisk config and any call/recording storage.
- Monitor disk usage if recordings are enabled later.
- Rate-limit public API endpoints at the reverse proxy.
- Keep `.env` and certificates outside the Git repository.

## Important testing boundary

This repository can be syntax-tested and container-configuration-tested in CI, but actual carrier calls, inbound DID delivery, remote WebRTC, NAT traversal and audio quality require a real SIP trunk/provider, real endpoint credentials, TLS certificates, and a reachable deployed VPS. Those environmental dependencies cannot truthfully be marked as validated from GitHub alone.
