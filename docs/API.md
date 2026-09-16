# EngineerIP Telephony API

The Flask telephony service is intended to be reachable from EngineerIP over the private Docker network. It listens on port `5000` inside the container; the production Compose configuration does not publish port 5000 to the public Internet.

## Base URL

From the EngineerIP CRM container:

```text
http://engineerip-telephony-api:5000
```

## Authentication

All `/api/v1/*` endpoints use the server-side token:

```http
Authorization: Bearer <TELEPHONY_TOKEN>
```

Never put the master `TELEPHONY_TOKEN`, ARI password, or IPComms credentials in browser JavaScript.

## Health

```http
GET /health
```

No authentication is required.

Example:

```bash
curl http://engineerip-telephony-api:5000/health
```

Returns HTTP `200` when the Flask service can reach Asterisk ARI. Returns HTTP `503` when ARI is unavailable or authentication fails.

## List calls

```http
GET /api/v1/calls
Authorization: Bearer <TELEPHONY_TOKEN>
```

Returns the calls currently held by the service's in-memory call store.

## Start outbound call

```http
POST /api/v1/calls
Content-Type: application/json
Authorization: Bearer <TELEPHONY_TOKEN>

{
  "contact_id": "582",
  "member_id": "37",
  "extension": "101",
  "phone": "+16235551234"
}
```

`phone` should use E.164 format such as `+16235551234`. `extension` defaults to the configured `DEFAULT_EXTENSION` and must contain only digits.

Example response:

```json
{
  "call": {
    "call_id": "uuid",
    "contact_id": "582",
    "member_id": "37",
    "extension": "101",
    "phone": "+16235551234",
    "direction": "outbound",
    "status": "initiated",
    "answered": false
  }
}
```

The API requests Asterisk to originate a `Local/<phone>@web-outbound` channel and records the returned UUID as the application call ID.

## Browser call launcher

```http
POST /api/v1/browser/call
Content-Type: application/json
Authorization: Bearer <TELEPHONY_TOKEN>

{
  "phone": "+16235551234",
  "extension": "101"
}
```

This endpoint uses the same server-side Bearer authentication in the current reference implementation. The included browser page is diagnostic/reference UI only; it does not embed the master token. Production EngineerIP should issue a short-lived, narrowly scoped capability or SIP credential after normal CRM authentication.

## Get call

```http
GET /api/v1/calls/<call_id>
Authorization: Bearer <TELEPHONY_TOKEN>
```

Returns HTTP `404` when the call ID is not in the current service store.

## Hang up

```http
POST /api/v1/calls/<call_id>/hangup
Authorization: Bearer <TELEPHONY_TOKEN>
```

The API requests Asterisk to hang up the application channel and updates the local call state.

## Set disposition

```http
POST /api/v1/calls/<call_id>/disposition
Authorization: Bearer <TELEPHONY_TOKEN>
Content-Type: application/json

{
  "disposition": "follow_up",
  "notes": "Asked us to call next Tuesday."
}
```

The disposition and notes are stored with the call and a `call.disposition` webhook is sent to the CRM when configured.

Suggested CRM disposition values are implementation-specific; examples include `reached`, `voicemail`, `gatekeeper`, `not_interested`, `follow_up`, `wrong_number`, and `remove_me`.

## Asterisk event intake

```http
POST /api/v1/webhooks/ari
Authorization: Bearer <TELEPHONY_TOKEN>
Content-Type: application/json
```

This is a controlled integration/test endpoint. The built-in ARI WebSocket listener already consumes Asterisk events, so Asterisk should not be wired to this HTTP endpoint and the listener simultaneously unless both paths are intentionally required.

## Errors

Typical responses:

- `400` — invalid/missing phone or invalid extension.
- `401` — missing or invalid Bearer token.
- `404` — requested call ID does not exist in the current call store.
- `502` — Asterisk API request failed while starting a call.
- `503` — `/health` cannot reach Asterisk ARI.

## CRM webhook events

When `CRM_WEBHOOK_URL` is configured, the service can emit:

```text
call.started
call.in_progress
call.ringing
call.answered
call.completed
call.hangup_requested
call.disposition
```

See `docs/WEBHOOKS.md` for the payload contract.
