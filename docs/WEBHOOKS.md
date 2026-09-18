# Webhook/Event Contract

Telephony emits normalized events to active database-managed webhook endpoints. Each endpoint has a URL, encrypted bearer token, active flag, and either `*` or a comma-separated event subscription. Manage these through `/admin` or the bearer-authenticated `/api/v1/webhooks` API. `CRM_WEBHOOK_URL` and `CRM_WEBHOOK_TOKEN` remain a first-run fallback only when no database webhook rows exist. The delivery `Authorization` header is `Bearer <endpoint token>`. The same secret signs the exact JSON body with HMAC-SHA256.

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
- `call.voicemail` (inbound call handed to extension voicemail)
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
    "caller_id_number": "+13025550103",
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

## Authentication and signature verification

Every delivery includes:

```http
Authorization: Bearer <webhook secret>
X-EngineerIP-Delivery: <unique UUID>
X-EngineerIP-Timestamp: <Unix seconds>
X-EngineerIP-Signature: sha256=<hex HMAC>
```

Compute `HMAC-SHA256(secret, timestamp + "." + raw_request_body)` and compare it to the signature with a constant-time comparison. Reject timestamps older than five minutes, require HTTPS outside a private network, and store `X-EngineerIP-Delivery` as a unique idempotency key. Signature verification must use the raw body bytes before JSON parsing.

## CRM handler guidance

1. Authenticate the bearer token.
2. Make the handler idempotent using `call_id + event` or a provider event identifier.
3. Upsert the call activity instead of creating duplicate activities on retries.
4. Use `answered=true` for reached/connect calculations and `duration_seconds` for call duration metrics.
5. Store the final disposition/notes from CRM separately from the telephony lifecycle.
6. Return HTTP 2xx only after the event has been accepted durably.
7. Treat telephony lifecycle events as factual state updates; do not derive a second duration/reporting engine in the telephony service.

## Failure behavior

Database-managed lifecycle events are written to a persistent SQLite outbox before delivery. The dedicated worker sends queued events every few seconds, treats non-2xx responses as failures, and retries with backoff up to five attempts. Delivery ID remains stable across retries. The admin/API test endpoint is immediate and is not queued. The legacy environment-only webhook remains best-effort, so production should migrate it into the admin-managed webhook list. CRM handlers must remain idempotent.

## Incoming calls

Configured DID routes enter the private ARI application. Telephony creates an inbound call record, emits `call.started` and `call.employee_ringing`, rings only the DID's owning extension, then emits answered/bridged/completed or failed events with `direction: inbound`. If the extension does not answer and voicemail is enabled, ARI returns the carrier channel to the private voicemail dialplan context. The generic unmatched `s` fallback remains a direct single-extension route and does not provide the full inbound CRM lifecycle; configure every production DID explicitly.

## Security

Do not expose this webhook publicly without authentication, TLS, replay protection and rate limiting. Prefer placing the Flask telephony API on the private Docker network and using a narrow internal route between EngineerIP and this service.
