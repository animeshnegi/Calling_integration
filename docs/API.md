# EngineerIP Telephony API

Base URL is the private Flask service URL. Keep it reachable only from the CRM/private network where possible.

## Authentication

CRM-to-telephony calls use:

```http
Authorization: Bearer $TELEPHONY_TOKEN
```

Never put this token in browser JavaScript.

## Health

```http
GET /health
```

Returns 200 when Flask can reach Asterisk ARI and 503 otherwise.

## Start outbound call

```http
POST /api/v1/calls
Content-Type: application/json
Authorization: Bearer <token>

{
  "contact_id": "582",
  "member_id": "37",
  "extension": "103",
  "phone": "+16235551234",
  "direction": "outbound"
}
```

The phone must be supplied in a normalized form accepted by the configured SIP provider; E.164 (`+countrycode...`) is recommended.

Response:

```json
{
  "call": {
    "call_id": "uuid",
    "contact_id": "582",
    "member_id": "37",
    "extension": "103",
    "phone": "+16235551234",
    "status": "initiated",
    "answered": false
  }
}
```

## Browser call launcher

The standalone browser page uses `/api/v1/browser/call`. It is authenticated with the same Bearer token in this reference implementation. For production, EngineerIP should issue a short-lived per-user capability token rather than embedding the master service token in the browser.

## Get call

```http
GET /api/v1/calls/<call_id>
Authorization: Bearer <token>
```

## Hang up

```http
POST /api/v1/calls/<call_id>/hangup
Authorization: Bearer <token>
```

## Set disposition

```http
POST /api/v1/calls/<call_id>/disposition
Authorization: Bearer <token>
Content-Type: application/json

{
  "disposition": "follow_up",
  "notes": "Asked us to call next Tuesday."
}
```

Suggested CRM disposition values are implementation-specific; common examples are `reached`, `voicemail`, `gatekeeper`, `not_interested`, `follow_up`, `wrong_number`, and `remove_me`.

## Asterisk event intake

```http
POST /api/v1/webhooks/ari
Authorization: Bearer <token>
Content-Type: application/json
```

This endpoint is intended for controlled integration tests or an external event relay. The built-in ARI listener already consumes Asterisk's event WebSocket; do not wire Asterisk to this endpoint and the listener simultaneously unless you deliberately want both paths.
