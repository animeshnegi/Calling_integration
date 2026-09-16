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

No authentication is required. It returns only whether Asterisk is reachable; it does not expose the Asterisk version or configured extension list.

## Extensions

```http
GET /api/v1/extensions
Authorization: Bearer <TELEPHONY_TOKEN>
```

Returns the configured extension numbers and default extension. SIP passwords are never returned.

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

`phone` must be E.164 format. `extension` must be a configured three-digit extension. If omitted, `DEFAULT_EXTENSION` is used.

The current call-originator implementation is still a POC and should not be treated as the final employee-first click-to-call flow until the ARI bridge logic is completed and tested.

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

This endpoint is for controlled integration/testing. The built-in ARI WebSocket listener already consumes Asterisk events.

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
- `503` — Asterisk unavailable for `/health`.

## CRM webhook events

When `CRM_WEBHOOK_URL` is configured in production, `CRM_WEBHOOK_TOKEN` is required and is sent as a Bearer token to the CRM.

Possible events:

```text
call.started
call.in_progress
call.ringing
call.answered
call.completed
call.hangup_requested
call.disposition
```
