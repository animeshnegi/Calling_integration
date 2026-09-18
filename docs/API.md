# EngineerIP Telephony API

The Flask telephony service is reachable from EngineerIP over the private Docker network. Port `5000` is not published publicly.

## Authentication

All `/api/v1/*` endpoints require:

```http
Authorization: Bearer <TELEPHONY_TOKEN>
```

Production retains `TELEPHONY_TOKEN` as an emergency/legacy master credential. Normal CRM integrations should create a separate `eip_...` key in **Admin > API keys** with least-privilege scopes. API keys are generated with cryptographic randomness, displayed once, stored only as SHA-256 hashes, track last use, and can be revoked. Invalid credentials return 401 and insufficient scopes return 403. Never put any API key, master token, ARI password, SIP password, or provider credential in browser JavaScript.

Available scopes are `calls:read`, `calls:write`, `config:read`, `recordings:read`, `voicemail:read`, `voicemail:write`, and `webhooks:manage`; `*` is full access.

The API has a lightweight per-client request limit and a separate outbound-call limit to reduce abuse and toll-fraud risk. A production reverse proxy/API gateway should enforce equivalent limits across multiple replicas.

## Health

```http
GET /health
```

No authentication is required. It returns whether Asterisk and the dedicated ARI event worker are ready.

## Extensions

```http
GET /api/v1/extensions
Authorization: Bearer <TELEPHONY_TOKEN>
```

Returns the configured active extension numbers and default extension. SIP passwords are never returned.

## Phone numbers and ownership

```http
GET /api/v1/numbers
GET /api/v1/numbers?extension=101
Authorization: Bearer <TELEPHONY_TOKEN>
```

Returns configured DIDs with provider, owning `inbound_extension`, active state and `default_outbound`. Use this before presenting caller-ID choices in a CRM. It never returns provider credentials.

## Start outbound call

```http
POST /api/v1/calls
Content-Type: application/json
Authorization: Bearer <CRM_API_KEY>
Idempotency-Key: <unique CRM request UUID>

{
  "contact_id": "582",
  "member_id": "37",
  "extension": "102",
  "phone": "+16235551234",
  "caller_id_number": "+13025550102"
}
```

`phone` must be E.164 format. `extension` must be a configured active three-digit extension. If omitted, the active configured default extension is used. `caller_id_number` is optional, but when supplied it must be an active DID assigned to that extension. Otherwise the extension's default/first assigned active number is used. Calls are rejected when the extension has no assigned callback number. See [`NUMBER_OWNERSHIP.md`](NUMBER_OWNERSHIP.md).

Send a unique `Idempotency-Key` (8–128 safe characters) for every user click/CRM job. The key is scoped to the API client for 24 hours. A successful replay returns HTTP 200 with the original call and `idempotent_replay: true`; a concurrent in-progress duplicate returns 409. This prevents network retries from originating duplicate paid calls.

The production call lifecycle is employee-first:

1. Persist the call and deterministic employee channel ID.
2. Originate the employee extension into the ARI application.
3. Wait for the employee channel to reach `Up`.
4. Persist the deterministic customer channel ID before originating the customer leg.
5. Originate the customer through the selected SIP provider.
6. Wait for the customer channel to reach `Up`.
7. Create a deterministic mixing bridge and add both channels.
8. Mark the call answered and persist `answered_at`.
9. Start bridge recording when enabled for the system and extension.
10. On channel destruction, hang up the other leg, stop recording, destroy the bridge, persist final duration/status and emit the completion webhook.

The ARI worker also reconciles persisted incomplete calls with live Asterisk channels after a worker restart.

## Browser call launcher

`POST /api/v1/browser/call` is **disabled by default**. Enable it only after implementing short-lived, narrowly scoped browser credentials. The master `TELEPHONY_TOKEN` must not be exposed to browser JavaScript.

## Get / hang up / disposition

```http
GET  /api/v1/calls/<call_id>
POST /api/v1/calls/<call_id>/hangup
POST /api/v1/calls/<call_id>/disposition
```

All require the Bearer token. Call IDs are syntactically validated before lookup. Disposition and notes have length limits.

## Asterisk event intake

```http
POST /api/v1/webhooks/ari
Authorization: Bearer <TELEPHONY_TOKEN>
Content-Type: application/json
```

This endpoint is disabled by default (`ENABLE_ARI_WEBHOOK=false`) and requires full access when deliberately enabled for isolated testing. Keep it disabled in production. The private ARI WebSocket worker is the normal event consumer.

## Security limits

- Maximum request body: 64 KiB.
- General authenticated API rate limit: 120 requests/minute per client address.
- Outbound call creation limit: 30 requests/minute per client address.
- Phone numbers are restricted to E.164 syntax before reaching Asterisk.
- Exceptions from Asterisk are logged server-side and are not returned to clients.
- Security response headers are added to Flask responses.
- Diagnostic UI is disabled by default.
- Browser call API is disabled by default.
- Asterisk ARI TCP 8088 is private and is not published.
- Asterisk WebRTC WSS port 8089 is not published by the current Compose configuration until trusted TLS/reverse-proxy access is ready.

## Errors

Typical responses:

- `400` — invalid JSON, phone, extension, or disposition data.
- `401` — missing, invalid, expired/revoked Bearer credential.
- `403` — authenticated API key lacks the endpoint scope.
- `404` — call not found or disabled endpoint.
- `429` — API or outbound-call rate limit exceeded.
- `502` — Asterisk unavailable while starting a call.
- `503` — Asterisk/ARI worker unavailable for `/health` or outbound calls.

## CRM webhook events

When `CRM_WEBHOOK_URL` is configured in production, `CRM_WEBHOOK_TOKEN` is required and is sent as a Bearer token to the CRM.

The service emits lifecycle events including:

```text
call.started
call.employee_ringing
call.employee_answered
call.customer_dialing
call.bridged
call.answered
call.recording_started
call.recording_finished
call.recording_failed
call.recording_announcement_failed
call.recording_deleted
call.completed
call.failed
call.hangup_requested
call.disposition
```

CRM handlers should treat `call_id` as the stable identifier and make webhook processing idempotent.

## Recordings

List calls that have recording metadata:

```http
GET /api/v1/recordings
Authorization: Bearer <TELEPHONY_TOKEN>
```

Stream a finalized recording by its **call ID**:

```http
GET /api/v1/recordings/<call_id>/file
Authorization: Bearer <TELEPHONY_TOKEN>
Range: bytes=0-
```

The response uses `Content-Disposition: inline`. HTTP range requests are passed to private ARI so compatible clients can seek. A recording is available only after its status is `finalized`; deleted, failed, active, or unknown recordings return 404. Do not put the master bearer token in a public HTML audio element. CRM should authorize its user server-side and proxy this endpoint when browser playback is required.

Example download:

```bash
curl --fail --location \
  -H "Authorization: Bearer $TELEPHONY_TOKEN" \
  -o call.wav \
  "http://telephony-api:5000/api/v1/recordings/$CALL_ID/file"
```

## Webhook management

Webhook responses never return the stored bearer token; `has_token` indicates whether one exists.

```http
GET /api/v1/webhooks
Authorization: Bearer <TELEPHONY_TOKEN>
```

Create a webhook:

```http
POST /api/v1/webhooks
Authorization: Bearer <TELEPHONY_TOKEN>
Content-Type: application/json

{
  "name": "Production CRM",
  "url": "https://crm.example.com/api/telephony/events",
  "token": "a-long-random-shared-secret",
  "events": "call.started,call.answered,call.completed,call.failed",
  "active": true
}
```

Use `"events": "*"` for all call events. To update, send the returned `webhook_id` as `id`. Leave `token` empty during an update to retain the existing encrypted token.

Test delivery:

```http
POST /api/v1/webhooks/<webhook_id>/test
Authorization: Bearer <TELEPHONY_TOKEN>
```

The receiver gets:

```json
{
  "event": "webhook.test",
  "sent_at": "2026-09-18T12:00:00+00:00",
  "source": "engineerip-telephony"
}
```

Delete:

```http
DELETE /api/v1/webhooks/<webhook_id>
Authorization: Bearer <TELEPHONY_TOKEN>
```

Webhook configuration can also be managed in `/admin`. Database webhook tokens are encrypted using a key derived from `SECRET_KEY`. When one or more database webhooks exist they replace the legacy single `CRM_WEBHOOK_URL`; the environment URL remains a bootstrap fallback only while the database list is empty.

## Voicemail API

Extension mailbox messages can be listed, played, marked read, and deleted through bearer-authenticated endpoints:

```text
GET    /api/v1/voicemail/mailboxes
GET    /api/v1/voicemails?extension=101&folder=inbox
GET    /api/v1/voicemails/101/inbox/msg0000/file
POST   /api/v1/voicemails/101/inbox/msg0000/read
DELETE /api/v1/voicemails/101/old/msg0000
```

Valid folders are `inbox`, `old`, and `urgent`. Audio supports HTTP conditional/range delivery. Mailbox PINs are write-only admin values and are never included in responses. See [`VOICEMAIL.md`](VOICEMAIL.md) for call flow, response fields, storage, phone access, security, and troubleshooting.
