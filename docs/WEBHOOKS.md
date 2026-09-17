# Webhook/Event Contract

Telephony emits normalized events to `CRM_WEBHOOK_URL` when configured. The `Authorization` header is `Bearer <CRM_WEBHOOK_TOKEN>`.

## Event names

The current service can emit:

- `call.started`
- `call.employee_ringing`
- `call.employee_answered`
- `call.customer_dialing`
- `call.bridged`
- `call.answered`
- `call.recording_started`
- `call.recording_finished`
- `call.recording_failed`
- `call.recording_announcement_failed`
- `call.recording_deleted`
- `call.completed`
- `call.failed`
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
    "provider": "IPComms",
    "direction": "outbound",
    "status": "answered",
    "answered": true,
    "started_at": "2026-09-10T17:30:00+00:00",
    "answered_at": "2026-09-10T17:30:07+00:00",
    "ended_at": null,
    "duration_seconds": 7,
    "employee_channel_id": "uuid-employee",
    "customer_channel_id": "uuid-customer",
    "bridge_id": "bridge-uuid",
    "recording_name": "call-uuid",
    "recording_format": "wav",
    "recording_status": "recording",
    "recording_path": null,
    "disposition": null,
    "notes": null
  }
}
```

## CRM handler guidance

1. Authenticate the bearer token.
2. Make the handler idempotent using `call_id + event` or a provider event identifier.
3. Upsert the call activity instead of creating duplicate activities on retries.
4. Use `answered=true` for reached/connect calculations and `duration_seconds` for call duration metrics.
5. Store the final disposition/notes from CRM separately from the telephony lifecycle.
6. Return HTTP 2xx only after the event has been accepted durably.
7. Treat telephony lifecycle events as factual state updates; do not derive a second duration/reporting engine in the telephony service.

## Failure behavior

Webhook delivery is currently best-effort: the telephony service sends the event with a short HTTP timeout and logs the request failure without retrying it. If durable delivery/retry is required for production, the CRM integration should add an outbox/queue or the telephony service should persist outbound webhook attempts before claiming durable delivery.

## Incoming calls

The current Asterisk dialplan routes inbound provider calls directly to the configured extension. The repository does **not yet implement an inbound CRM webhook lifecycle** equivalent to the outbound ARI flow. Therefore `CRM_INTEGRATION.md` should be treated as the intended future inbound mapping, not as an implemented guarantee.

## Security

Do not expose this webhook publicly without authentication, TLS, replay protection and rate limiting. Prefer placing the Flask telephony API on the private Docker network and using a narrow internal route between EngineerIP and this service.
