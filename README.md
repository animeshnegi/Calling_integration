# EngineerIP Telephony Integration

Standalone Asterisk + Flask telephony service for EngineerIP CRM.

## Current architecture

```text
EngineerIP CRM
      |
      | private Docker network (crm-network)
      v
telephony-api:5000  <---->  telephony-ari worker
      |                         |
      | private Docker network  | ARI WebSocket
      v                         v
                    Asterisk / PJSIP
                         |
                         +--> Zoiper / SIP phones
                         |
                         +--> IPComms SIP trunk --> PSTN
```

Asterisk is the telephony engine. The Flask service provides the CRM-facing API. A dedicated ARI worker owns the single ARI WebSocket connection and processes call lifecycle events. ARI and AMI stay private on the telephony Docker network and are not published to the Internet.

## Goals

- Asterisk in its own Docker container.
- Flask API and a dedicated ARI worker in separate containers.
- Private CRM ↔ telephony API ↔ Asterisk ARI communication.
- Multiple SIP hard/soft-phone extensions.
- Browser calling foundation with WebRTC/WSS, disabled until trusted TLS is deployed.
- Outbound click-to-call initiated by CRM.
- Employee extension rings first; the customer leg is created only after employee answer.
- Employee and customer legs are placed into a mixing bridge after customer answer.
- Optional bridge recording with retention managed through private ARI.
- Persistent call state in SQLite so worker restarts do not erase call metadata.
- Call lifecycle events, answer status and answered-call duration tracking.
- Optional CRM webhook integration.
- SIP-trunk-ready configuration with provider IP/CIDR allowlists for inbound SIP.
- Safe-by-default configuration with no public ARI/AMI exposure.

## Repository layout

```text
Calling_integration/
├── app/
│   ├── __init__.py
│   ├── __main__.py
│   ├── ari_worker.py
│   ├── asterisk_client.py
│   ├── ami.py
│   ├── admin.py
│   ├── config.py
│   ├── models.py
│   ├── routes.py
│   ├── services.py
│   └── telephony_config.py
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
│   ├── test_api.py
│   ├── test_asterisk_client.py
│   ├── test_call_store.py
│   ├── test_services.py
│   └── test_telephony.py
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── requirements.txt
└── docs/
    ├── API.md
    ├── API_TESTING.md
    ├── WEBHOOKS.md
    ├── ASTERISK.md
    ├── WEBRTC.md
    └── DEPLOYMENT.md
```

## Multiple extensions

Local SIP extensions are configuration-driven. The administration database manages active extensions and their encrypted SIP credentials. The Asterisk container receives the rendered configuration through the private shared configuration volume.

The API only accepts an active, configured extension. This prevents a CRM request from attempting to originate through an undefined endpoint.

The authenticated extension list is available from:

```text
GET /api/v1/extensions
```

Browser/WebRTC configuration is intentionally disabled by default until a trusted WSS/TLS path is deployed.

## Outbound call flow

The intended outbound lifecycle is:

1. CRM calls `POST /api/v1/calls`.
2. API validates the E.164 destination and active employee extension.
3. A call record is persisted **before** ARI originate to avoid losing an immediate `StasisStart` event.
4. Asterisk rings the employee extension.
5. After the employee answers, ARI originates the customer leg through the selected SIP provider.
6. After the customer answers, ARI creates a mixing bridge and joins both channels.
7. If recording is enabled globally and for the employee extension, bridge recording starts.
8. Recording-finished and channel lifecycle events update the persistent call record and CRM webhooks.
9. On hangup, the bridge is destroyed and the final call status/duration is persisted.

A mixing bridge is used because Asterisk's bridge recording captures the mixed audio from the bridge participants. urlAsterisk bridge recording documentationhttps://docs.asterisk.org/Latest_API/API_Documentation/Asterisk_REST_Interface/Bridges_REST_API/

## API endpoints

```text
GET  /health
GET  /api/v1/extensions
GET  /api/v1/providers
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

The API rejects outbound calls while the dedicated ARI event worker is not connected. This prevents a call from entering Stasis when no event consumer is ready.

## IPComms / Asterisk operation

The production/POSIX deployment uses IPComms as the SIP provider. Credentials are supplied through `.env` for first-run bootstrap and then stored encrypted in the telephony settings database. They are never committed to Git.

Provider inbound traffic is matched using explicit IP/CIDR allowlists rendered as PJSIP `identify` objects. Asterisk documents IP-based endpoint identification as the mechanism for associating inbound provider traffic with a configured endpoint. urlAsterisk PJSIP endpoint identification documentationhttps://docs.asterisk.org/Configuration/Channel-Drivers/SIP/Configuring-res_pjsip/Asterisk-PJSIP-Troubleshooting-Guide/

## Recording

Recordings are stored by Asterisk in the persistent recording volume. The Flask/API containers do not mount the recording volume. Retention is enforced by the ARI worker through the private Asterisk recordings API, which supports listing and deleting completed stored recordings. urlAsterisk recordings API documentationhttps://docs.asterisk.org/Certified-Asterisk_20.7_Documentation/API_Documentation/Asterisk_REST_Interface/Recordings_REST_API/

Recording settings include:

- global enable/disable;
- per-extension recording enable/disable;
- WAV/GSM-family format selection supported by the deployment;
- maximum recording duration;
- retention period;
- optional recording beep;
- optional announcement playback using an Asterisk `sound:` or `recording:` media URI.

## Docker deployment

Before deployment:

```bash
cp .env.example .env
# edit .env with real secrets, public address, extension passwords and provider details

docker compose config
docker compose build --no-cache
docker compose up -d
docker compose ps
```

The Flask API is served by Gunicorn. The ARI event listener is deliberately a separate container so multiple Gunicorn workers cannot create duplicate ARI event consumers.

Asterisk ARI 8088, AMI 5038 and WSS 8089 are not published by Compose. SIP 5060/UDP and the configured RTP range are the only Asterisk host ports published for the telephony path.

## Secrets

Never commit `.env`, SIP passwords, ARI passwords, CRM tokens, or private TLS keys. `.gitignore` excludes `.env` and Asterisk private keys.

Use strong unique values for:

- `SECRET_KEY`
- `TELEPHONY_TOKEN`
- `CRM_WEBHOOK_TOKEN`
- `ASTERISK_ARI_PASSWORD`
- `ASTERISK_AMI_PASSWORD`
- `EXTENSION_<number>_PASSWORD` for every configured SIP extension
- `IPCOMMS_SIP_PASSWORD`

Rotate any credentials that were previously exposed in logs, screenshots, source code, or chat history.

## Validation

The repository has GitHub Actions coverage for the Python test suite. Run locally before deployment:

```bash
python -m pytest -q
python -m compileall app
```

Validate the Compose file with:

```bash
docker compose config
```

For real telephony validation, verify provider registration, each Zoiper/SIP extension registration, employee-first outbound calling, customer-leg origination after employee answer, two-party audio, recording creation/finalization, inbound DID delivery, RTP/audio, and CRM lifecycle webhooks on the deployed VPS.

## Security boundary

Do not expose Asterisk ARI TCP 8088 or AMI TCP 5038 publicly. SIP/RTP ports are exposed only as required for device/provider connectivity. The browser API remains disabled until a deliberate short-lived credential and trusted WSS/TLS design is deployed.
