# EngineerIP Telephony Integration

Standalone Asterisk + Flask telephony service for EngineerIP CRM.

## Current architecture

```text
EngineerIP CRM
      |
      | private Docker network (crm-network)
      v
telephony-api:5000
      |
      | private Docker network
      v
Asterisk / PJSIP
      |
      +--> Zoiper / SIP phones
      |
      +--> IPComms SIP trunk --> PSTN
```

Asterisk is the telephony engine. The Flask service provides the CRM-facing API and receives/normalizes telephony lifecycle events. ARI stays private on the Docker network and is not published to the Internet.

## Goals

- Asterisk in its own Docker container.
- Flask integration/API service in its own container.
- Private CRM ↔ telephony API ↔ Asterisk ARI communication.
- SIP hard/soft phones using PJSIP.
- Browser calling foundation with WebRTC/WSS.
- Outbound click-to-call initiated by CRM.
- Inbound call delivery to the configured extension.
- Call lifecycle events, answer status and duration tracking.
- Optional CRM webhook integration.
- SIP-trunk-ready configuration.
- Safe-by-default configuration with no public ARI exposure.

## Repository layout

```text
Calling_integration/
├── app/
│   ├── __init__.py
│   ├── __main__.py
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

## API endpoints

The Flask service provides these CRM-facing endpoints:

```text
GET  /health
GET  /api/v1/calls
POST /api/v1/calls
POST /api/v1/browser/call
GET  /api/v1/calls/<call_id>
POST /api/v1/calls/<call_id>/hangup
POST /api/v1/calls/<call_id>/disposition
POST /api/v1/webhooks/ari
```

All `/api/v1/*` endpoints require:

```http
Authorization: Bearer <TELEPHONY_TOKEN>
```

The `/health` endpoint is unauthenticated and returns HTTP 200 when the telephony API can reach Asterisk ARI, otherwise HTTP 503.

### Start a call from EngineerIP CRM

```http
POST /api/v1/calls
Authorization: Bearer <TELEPHONY_TOKEN>
Content-Type: application/json

{
  "contact_id": "582",
  "phone": "+16235551234",
  "extension": "101",
  "member_id": "37"
}
```

The service creates a call identifier, requests Asterisk to originate the call, stores the call state, and sends a `call.started` CRM webhook when configured.

See [`docs/API.md`](docs/API.md) for the complete contract and examples.

## IPComms / Asterisk operation

The production/POSIX deployment uses IPComms as the SIP provider. Credentials are supplied only through `.env`; they are never stored in Git.

Extension `101` is the current Zoiper/SIP-phone test endpoint. The endpoint uses `ulaw,alaw` for the carrier-compatible SIP leg. The browser WebRTC endpoint can use Opus/ulaw/alaw separately.

The configured IPComms DID is used by the generated Asterisk dialplan for inbound calls. The provider source IP allow-list is required so inbound SIP is identified by provider source address.

## Docker deployment

For a normal host with enough build resources:

```bash
cp .env.example .env
# edit .env

docker compose config
docker compose up -d --build
docker compose ps
```

For the low-memory VPS workflow used by this project, build the Asterisk image on the Windows Docker host, export it, transfer the TAR to the VPS, and load it there. Build/redeploy the Flask `telephony-api` image separately on the VPS or another build host as appropriate.

The `telephony-api` service listens on port 5000 inside the Docker network. It is intentionally not published as a public host port. EngineerIP CRM should reach it at:

```text
http://engineerip-telephony-api:5000
```

Asterisk ARI uses:

```text
http://asterisk:8088/ari
```

and remains private to the telephony Docker network.

## Secrets

Never commit `.env`, SIP passwords, ARI passwords, CRM tokens, or private TLS keys. `.gitignore` excludes `.env` and Asterisk private keys.

Use strong unique values for:

- `SECRET_KEY`
- `TELEPHONY_TOKEN`
- `CRM_WEBHOOK_TOKEN`
- `ASTERISK_ARI_PASSWORD`
- `EXTENSION_101_PASSWORD`
- `WEBRTC_EXTENSION_PASSWORD`
- `IPCOMMS_SIP_PASSWORD`

## Validation

Run the application tests before deployment:

```bash
python -m pytest -q
python -m compileall app
```

Validate the Compose file with:

```bash
docker compose config
```

For real telephony validation, verify IPComms registration, Zoiper registration, outbound calling, inbound DID delivery, RTP/audio, and then CRM API → Asterisk call control on the deployed VPS.

## Security boundary

Do not expose Asterisk ARI TCP 8088 publicly. SIP/RTP ports are exposed only as required for device/provider connectivity. TCP 5000 should remain private unless a deliberate reverse-proxy/API security design is added.

The standalone browser page is a reference/diagnostic UI. Production EngineerIP browser calling should use short-lived, narrowly scoped browser credentials rather than exposing the master telephony token or any SIP-provider credentials to JavaScript.
