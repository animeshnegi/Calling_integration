# EIP Telephony Control

A multi-tenant Asterisk + Flask platform for managed US business numbers, calling, voicemail and CRM automation.

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
- Persistent customer, configuration, billing, and call state in MySQL, with automatic table creation and pooled connection health checks.
- Deterministic channel IDs are persisted before each originate so fast ARI events can be correlated even before the originate request returns.
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

## Administration console

Open `/` for the public EIP Telephony Control landing, $5-per-number pricing, signup, and customer login. See [`docs/MULTI_TENANCY_AND_BILLING.md`](docs/MULTI_TENANCY_AND_BILLING.md) for resource ownership, carrier provisioning, tenant isolation, invoices, number discontinuation, and the secure `engineerip` customer provisioning command.

Open `/admin` through the recommended HTTPS reverse proxy to use the responsive telephony control center. Its sidebar separates Dashboard, Extensions, Phone Numbers, SIP Providers, Call History, extension-grouped Recordings, CRM Webhooks, Call Settings, and Security. Administrators can add multiple DIDs, assign each number to one active extension, select per-extension outbound caller IDs, manage the scoped CRM API keys and signed CRM webhooks a customer created (an administrator never creates one for them), search call history, play finalized recordings, manage extension voicemail inboxes, configure SendGrid attachment delivery, create extension-scoped users, and edit a customer's call flows. Default outbound and inbound fallback extensions belong to the customer, who sets them on their Numbers page; recording is switched per device. Customer callbacks to a known DID ring only its owning extension; see [`docs/NUMBER_OWNERSHIP.md`](docs/NUMBER_OWNERSHIP.md). A signed-in account can open `/documentation` for the setup, REST API and webhook reference of this deployment, linked from the top of APIs & Webhooks; each extension registers with a generated SIP identity (`kqzvhd_101`), shown with the rest of its credentials in one copyable sheet. For the endpoint scope matrix, HMAC verification, durable webhook flow and production checklist, see [`docs/CRM_SECURITY_AUDIT.md`](docs/CRM_SECURITY_AUDIT.md). Voicemail can be enabled per extension with a private numeric PIN; users dial `*97` from their registered phone to enter their mailbox. See [`docs/EMAIL_AND_USERS.md`](docs/EMAIL_AND_USERS.md) for SendGrid and role-based access.

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
3. A call record and deterministic employee channel ID are persisted **before** ARI originate to avoid losing an immediate event.
4. Asterisk rings the employee extension.
5. After the employee answers, ARI persists the deterministic customer channel ID before originating the customer leg.
6. After the customer answers, ARI creates a mixing bridge and joins both channels.
7. If recording is enabled globally and for the employee extension, bridge recording starts.
8. Recording-finished and channel lifecycle events update the persistent call record and CRM webhooks.
9. On hangup, the bridge is destroyed and the final call status/duration is persisted.

Asterisk's ARI channel originate API creates the channel immediately and supports caller-selected channel IDs and channel variables in the request body. See the [Asterisk Channels REST API](https://docs.asterisk.org/Latest_API/API_Documentation/Asterisk_REST_Interface/Channels_REST_API/).

## API endpoints

```text
GET  /health
GET  /api/v1/extensions
GET  /api/v1/numbers
GET  /api/v1/providers
GET  /api/v1/calls
GET  /api/v1/recordings
GET  /api/v1/recordings/<call_id>/file
GET  /api/v1/voicemail/mailboxes
GET  /api/v1/voicemails
GET  /api/v1/voicemails/<extension>/<folder>/<message>/file
POST /api/v1/voicemails/<extension>/<folder>/<message>/read
DELETE /api/v1/voicemails/<extension>/<folder>/<message>
GET  /api/v1/webhooks
POST /api/v1/webhooks
POST /api/v1/webhooks/<id>/test
DELETE /api/v1/webhooks/<id>
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

Provider inbound traffic is matched using explicit IP/CIDR allowlists rendered as PJSIP `identify` objects. Asterisk documents IP-based endpoint identification as the mechanism for associating inbound provider traffic with a configured endpoint.

## Recording

Call recording is **off by default** globally and for every new extension. The administrator controls a master switch; when it is off, no administrator or user preference can start a recording. When the master switch is on, each assigned extension user can opt their own extension in or out from the Security page, and administrators can manage every extension's preference.

Recordings are stored by Asterisk in the persistent recording volume. The Flask/API containers do not mount the recording volume. Retention is enforced by the ARI worker through the private Asterisk recordings API. Finalized recordings can be listed and securely streamed with the bearer-authenticated recording API, or played in the authenticated admin console; the underlying volume is never published.

Recording settings include:

- global enable/disable;
- per-extension recording enable/disable;
- WAV/GSM-family format selection supported by the deployment;
- maximum recording duration;
- retention period;
- optional recording beep;
- optional announcement playback using an Asterisk `sound:` or `recording:` media URI.

## Database

Production uses the `DATABASE_URI` value from `.env`, for example:

```dotenv
DATABASE_URI=mysql+pymysql://eip_app:URL_ENCODED_PASSWORD@mysql.example.internal:3306/eip_telephony?charset=utf8mb4
```

The database and restricted MySQL user must exist; application tables and indexes are created automatically. See [`docs/MYSQL.md`](docs/MYSQL.md) for grants, URI encoding, TLS, connectivity verification, backups, and migration guidance.

## Docker deployment

Container logs are bounded to three compressed 10 MB files per service, and Asterisk file logs use a capped tmpfs. If an older deployment has already consumed disk, follow [`docs/LOGGING_AND_DISK.md`](docs/LOGGING_AND_DISK.md) and run the safe dry-run helper:

```bash
./scripts/cleanup-logs.sh
```

For the low-resource Google VM workflow, including cross-platform build, compressed image export, checksum verification, import without rebuilding, backups, rollback, admin operation, and troubleshooting, see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

Build and export on the personal computer:

```bash
./scripts/build-export-images.sh v1
```

Then copy the archive/repository to the VM and start without compiling:

```bash
./scripts/import-start-images.sh engineerip-telephony-v1.tar.gz v1
```

For a normal local build, before deployment:

```bash
cp .env.example .env
# edit .env: secrets, public address and the RTP range. Carrier credentials and
# extensions are configured in the console (/admin), not in the environment.

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
- `ADMIN_PASSWORD` (first-run administrator; change it in the console)
- `ASTERISK_ARI_PASSWORD`
- `ASTERISK_AMI_PASSWORD`
- `CRM_WEBHOOK_TOKEN`, if a CRM endpoint is configured
- the SIP passwords the console generates per extension, once handed to a device

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
