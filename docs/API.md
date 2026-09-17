# EngineerIP Telephony API

The Flask telephony service is reachable from EngineerIP over the private Docker network. Port `5000` is not published publicly.

## Authentication

All `/api/v1/*` endpoints require:

```http
Authorization: Bearer <TELEPHONY_TOKEN>
```

Production requires a random `TELEPHONY_TOKEN` of at least 32 characters. Authentication uses constant-time token comparison. Never put the master token, ARI password, SIP passwords, or provider credentials in browser JavaScript.

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

## Start outbound call

```http
POST /api/v1/calls
Content-Type: application/json
Authorization: Bearer <TELEPHONY_TOKEN>

{
  "contact_id": "582",
  "member_id": "37",
  "extension": "102",
  "phone": "+16235551234"
}
```

`phone` must be E.164 format. `extension` must be a configured active three-digit extension. If omitted, the active configured default extension is used.

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

This endpoint is for controlled integration/testing. The built-in ARI WebSocket listener is the normal event consumer.

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
- `401` — missing or invalid Bearer token.
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
