# Telephony Control

A **standalone**, multi-tenant Asterisk + Flask platform for business phone
numbers, calling, routing, voicemail, recordings and a browser softphone.

It is not an extension of any other product and shares no Docker network with
one. Your CRM, helpdesk or internal tool integrates over HTTPS through the
bearer-authenticated REST API or signed webhooks.

## Architecture

```text
Your systems (any CRM / app)
      |  HTTPS: REST API + signed webhooks
      v
reverse proxy ──> telephony-api :5000  <──>  telephony-ari worker
      |                  |                        |
      |                  +── MySQL ───────────────+
      |                                           | ARI WebSocket
      v                                           v
  /ws (WSS)                              Asterisk / PJSIP
                                                 |
                                    +------------+------------+
                                    |                         |
                            desk / softphones          SIP trunk (PSTN)
```

Asterisk is the telephony engine. The Flask service provides the API and the
console. A dedicated ARI worker owns the single ARI WebSocket connection so
multiple Gunicorn workers cannot create duplicate event consumers. ARI and AMI
stay on the private `telephony-internal` bridge and are never published to the
Internet. Both Compose networks are local bridges - **no external network has
to exist** before `docker compose up`.

## Highlights

- Asterisk in its own container, built from a pinned upstream release.
- Flask API and a dedicated ARI event worker in separate containers.
- Multiple SIP hard/soft-phone extensions, plus a WebRTC browser phone.
- Outbound click-to-call from the API: employee extension rings first, the
  customer leg is created only after answer, then both join a mixing bridge.
- Inbound DID routing, phone menus (IVR), forwarding, voicemail and groups.
- Optional recording: a platform veto plus a per-extension opt-in.
- Persistent customer, configuration, billing and call state in MySQL.
- Deterministic channel IDs persisted before every originate so fast ARI
  events can be correlated.
- Signed CRM-agnostic webhooks and scoped API keys with idempotency.
- Safe by default: ARI/AMI are not exposed; the browser API is opt-in.

## Repository layout

```text
app/
├── __init__.py            app factory (wires services, routes, workers)
├── config.py              environment-driven configuration
├── admin.py               legacy import compatibility alias
├── services.py            call state machine and ARI event handling
├── database.py            MySQL/SQLite abstraction and schema repair
├── local_transcription.py offline voicemail transcription
├── manage_customer.py     interactive customer provisioning CLI
├── voicemail*.py          mailbox store and email delivery
├── models/                Call dataclass + CallStore, split by concern
│   ├── call.py
│   ├── settings.py        settings, users, billing, extension identities
│   └── store.py
├── routes/                HTTP surface
│   ├── admin.py           admin/customer routes and session handling
│   └── api.py
├── asterisk/              everything that talks to Asterisk
│   ├── client.py          ARI REST client + event loop
│   ├── ami.py             AMI client
│   ├── config_sync.py     renders PJSIP / dialplan / voicemail from the DB
│   └── worker.py          standalone ARI worker and boot backoff
├── templates/             all HTML pages
│   ├── index.html  admin.html  admin-login.html
│   ├── documentation.html  console-check.html  phone.html
└── static/
    ├── css/               tailwind.css (generated), phone.css (dialpad)
    ├── js/                admin.js, phone.js, app.js, jssip.min.js, sw.js
    ├── images/            bundled logo / favicon
    └── manifest.json      softphone PWA manifest

asterisk/                  Asterisk image, entrypoint and static config
docs/                      API, deployment, operations, WebRTC references
deploy/                    release checklist and host tools
scripts/                   build/import/verify helpers
tools/                     jsdom + HTTP verification harnesses
tests/                     pytest suite
src/tailwind.css           Tailwind input (design system)
tailwind.config.js         Tailwind content + theme
package.json               `npm run css` builds static/css/tailwind.css
```

HTML lives in `templates/`; executable page scripts, stylesheets and images live
under `static/`. `models/settings.py` owns settings persistence and shared
account/extension identity helpers without importing Flask. `routes/admin.py`
owns the admin/customer HTTP surface, sessions and login throttling. ARI, AMI,
configuration rendering and the background worker live under `asterisk/`.

Legacy imports (`app.admin`, `app.asterisk_client`, `app.ami`,
`app.telephony_config`) and `python -m app.ari_worker` remain supported.
The canonical worker command is `python -m app.asterisk.worker`; both commands
run the same implementation, including startup retry/backoff and recording
reconciliation. No database schema or SIP/routing identifiers change.

The softphone loads bundled JsSIP first, then a synchronous static fallback
loader before `phone.js`. That loader retains the pinned upstream URL only for
the existing recovery case where the local library fails to load.

## UI (Tailwind)

All pages use Tailwind CSS. The stylesheet is **built once and committed**, so
the deployment host needs neither Node nor a CDN:

```bash
npm install
npm run css          # writes app/static/css/tailwind.css
```

`src/tailwind.css` holds the `@tailwind` directives plus a small component
layer composed with `@apply` for the console's semantic class names. The
softphone dialpad keeps a small dedicated `static/css/phone.css` for its
specialised layout. Rebuild the CSS after editing templates or `admin.js`.

## Integration

All `/api/v1/*` endpoints require:

```http
Authorization: Bearer <TELEPHONY_TOKEN or a scoped API key>
```

```text
GET  /health
GET  /api/v1/extensions      GET  /api/v1/numbers
GET  /api/v1/providers       GET  /api/v1/calls
GET  /api/v1/recordings      GET  /api/v1/recordings/<call_id>/file
GET  /api/v1/voicemails      GET  /api/v1/voicemail/mailboxes
POST /api/v1/calls           GET  /api/v1/calls/<call_id>
POST /api/v1/calls/<call_id>/hangup
POST /api/v1/calls/<call_id>/disposition
GET  /api/v1/webhooks        POST /api/v1/webhooks
POST /api/v1/webhooks/<id>/test    DELETE /api/v1/webhooks/<id>
```

Webhooks are signed with HMAC-SHA256 over the exact JSON body, delivered with
an `Authorization: Bearer` header, queued durably and retried. See
[`docs/API.md`](docs/API.md), [`docs/WEBHOOKS.md`](docs/WEBHOOKS.md) and
[`docs/CRM_INTEGRATION.md`](docs/CRM_INTEGRATION.md). The API rejects outbound
calls while the ARI event worker is not connected.

## Outbound call flow

1. A client calls `POST /api/v1/calls`.
2. The API validates the E.164 destination and active extension.
3. A call record and deterministic employee channel ID are persisted **before**
   ARI originate, so an immediate event is never lost.
4. Asterisk rings the employee extension.
5. After the employee answers, the deterministic customer channel ID is
   persisted and the customer leg is originated.
6. After the customer answers, both channels join a mixing bridge.
7. If recording is enabled for the extension and allowed by the platform
   switch, bridge recording starts.
8. Lifecycle, answer and recording events update the call record and webhooks.
9. On hangup the recording is stopped **before** the channels are torn down,
   then the bridge is destroyed and the final status/duration is persisted.

## Recording

Recording is a customer's decision, per extension, with one platform switch
that can veto it everywhere. A new extension starts with its own switch off;
the platform switch never starts a recording on its own. Audio is written by
Asterisk into a private volume that the Flask containers do not mount;
finalised recordings are streamed through private ARI to authenticated
clients. Retention is enforced by the ARI worker from the persisted call end
time. See [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for the failure-runbook.

## Database

```dotenv
DATABASE_URI=mysql+pymysql://eip:URL_ENCODED_PASSWORD@mysql.example.internal:3306/telephony?charset=utf8mb4
```

The database and a restricted MySQL user must exist; tables and indexes are
created automatically. See [`docs/MYSQL.md`](docs/MYSQL.md).

**Existing Docker MySQL / 1Panel upgrades:** a hostname such as
`1Panel-mysql-H8JJ` only resolves on the MySQL container's Docker network. Set
`MYSQL_DOCKER_NETWORK=1panel-network` in `.env` and use both Compose files with
`docker compose -f docker-compose.yml -f docker-compose.local.yml ...`. The
optional override connects only the API/worker to MySQL; Asterisk stays isolated.
For XAMPP or remote MySQL, leave `MYSQL_DOCKER_NETWORK` unset.

## Deployment

**Existing installations:** Docker defaults use `telephony-control/*` images and
neutral container names. To retain older EngineerIP image/container names, set
the `TELEPHONY_*_IMAGE` and `TELEPHONY_*_CONTAINER` values in `.env`; no third
Compose file is needed. See [the naming migration guide](docs/DOCKER_NAMING_MIGRATION.md).


```bash
cp .env.example .env
# edit .env: secrets, public SIP address and RTP range.
# Carrier credentials and extensions are configured in the console, not env.

docker compose config
docker compose build --no-cache
docker compose up -d
docker compose ps
```

No external network is required for XAMPP or managed/remote MySQL. For a
1Panel MySQL container, configure `COMPOSE_FILE` as shown above; Compose will
load the optional override automatically.

The API is served by Gunicorn and published on `127.0.0.1:5000`; put an HTTPS
reverse proxy in front of it for remote access. Asterisk ARI 8088 and WSS 8089
are published on loopback only, for that proxy. SIP 5060/UDP and the configured
RTP range are the only Asterisk ports exposed for media. For the low-resource
VM workflow (build on a PC, import on the VM) see
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Secrets

Never commit `.env`, SIP passwords, ARI/AMI passwords, API keys or private TLS
keys. Use strong unique values for `SECRET_KEY`, `TELEPHONY_TOKEN`,
`ADMIN_PASSWORD`, `ASTERISK_ARI_PASSWORD`, `ASTERISK_AMI_PASSWORD` and any
webhook tokens. Rotate anything previously exposed in logs or screenshots.

## Validation

```bash
python -m pytest -q
python -m compileall app
npm run css        # regenerate the Tailwind stylesheet
docker compose config
```

GitHub Actions runs the Python suite on every push and pull request.

## Security boundary

Do not expose ARI TCP 8088 or AMI TCP 5038 publicly. Keep the API behind an
HTTPS reverse proxy, hand scoped API keys to integrations, and never put the
master token in a browser. The direct browser call API stays disabled until a
short-lived credential design is enabled deliberately.
