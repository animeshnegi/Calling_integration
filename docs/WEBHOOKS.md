# Webhook/Event Contract

Telephony emits normalized events to `CRM_WEBHOOK_URL` when configured. The `Authorization` header is `Bearer <CRM_WEBHOOK_TOKEN>`.

## Event names

- `call.started`
- `call.in_progress`
- `call.ringing`
- `call.answered`
- `call.completed`
- `call.hangup_requested`
- `call.disposition`

## Payload

```json
{
  "event": "call.answered",
  "call": {
    "call_id": "uuid",
    "contact_id": "582",
    "member_id": "37",
    "extension": "103",
    "phone": "+16235551234",
    "direction": "outbound",
    "status": "answered",
    "answered": true,
    "started_at": "2026-09-10T17:30:00+00:00",
    "answered_at": "2026-09-10T17:30:07+00:00",
    "ended_at": null,
    "duration_seconds": 7,
    "disposition": null,
    "notes": null
  }
}
```

## CRM handler guidance

1. Authenticate the bearer token.
2. Make the handler idempotent using `call_id + event` or a provider event identifier.
3. Upsert the call activity instead of creating duplicate activities on retries.
4. Use `answered=true` for reached/connect calculations and `duration_seconds`/`talk_seconds` for talk-time metrics.
5. Store the final disposition/notes from CRM separately from the telephony lifecycle.
6. Return HTTP 2xx only after the event has been accepted durably.

## Security

Do not expose this webhook publicly without authentication, TLS, replay protection and rate limiting. Prefer placing the Flask telephony API on the private Docker network and using a narrow internal route between EngineerIP and this service.
