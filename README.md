# EngineerIP Telephony Integration

Standalone Asterisk + Flask telephony service for EngineerIP CRM.

## Goals

- Asterisk in its own Docker container.
- Flask integration/API service in its own container.
- Private CRM ↔ Asterisk ARI communication.
- SIP hard/soft phones using PJSIP.
- Browser calling with WebRTC over secure WebSocket.
- Outbound click-to-call initiated by CRM.
- Inbound call events and CRM lookup hooks.
- Automatic call lifecycle events, duration and answer status.
- Optional CDR/CEL export hooks.
- SIP-trunk-ready configuration.
- Safe-by-default configuration: no public ARI exposure.

## Repository layout

```text
Calling_integration/
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── asterisk.py
│   ├── models.py
│   ├── routes.py
│   └── services.py
├── asterisk/
│   ├── Dockerfile
│   ├── entrypoint.sh
│   └── config/
│       ├── asterisk.conf
│       ├── ari.conf
│       ├── extensions.conf
│       ├── http.conf
│       ├── logger.conf
│       ├── modules.conf
│       ├── pjsip.conf
│       └── rtp.conf
├── web/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── tests/
│   └── test_api.py
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── requirements.txt
└── docs/
    ├── API.md
    ├── WEBHOOKS.md
    ├── ASTERISK.md
    ├── WEBRTC.md
    └── DEPLOYMENT.md
```

## Important deployment note

This repository provides a complete, configurable telephony foundation, but PSTN calling requires a real SIP trunk/provider and valid credentials. A browser also requires HTTPS for WebRTC in normal production browser environments. Do not commit provider credentials or SIP passwords.

## Quick start

```bash
cp .env.example .env
# edit .env

docker compose up -d --build

docker compose ps
```

The Flask integration service listens on port 5000. Asterisk ARI is reachable only from the Docker network by default. SIP and RTP ports are exposed for devices/provider connectivity. Browser WebRTC uses the Asterisk HTTPS/WSS endpoint through the configured public hostname/reverse proxy.

## CRM integration

Use the Flask endpoint:

```http
POST /api/v1/calls
Authorization: Bearer <CRM_TELEPHONY_TOKEN>
Content-Type: application/json

{
  "contact_id": "582",
  "phone": "+16235551234",
  "extension": "103",
  "member_id": "37",
  "direction": "outbound"
}
```

The service requests an outbound call through Asterisk and returns a call identifier. Asterisk lifecycle events are normalized into webhook-style events and can be forwarded to EngineerIP.

See `docs/API.md`, `docs/WEBHOOKS.md`, and `docs/DEPLOYMENT.md` for exact settings.

## Local validation

```bash
python -m pytest -q
python -m compileall app
```

For full telephony validation you need a reachable Asterisk instance plus SIP endpoints and, for PSTN, a provider trunk.
