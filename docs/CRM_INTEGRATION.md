# EIP CRM Integration

This service is intentionally CRM-agnostic. The existing EngineerIP CRM should remain the source of truth for members, contacts, activity rows, outcomes and reporting. Telephony only supplies factual call lifecycle data.

## Recommended member mapping

Store a telephony extension against each CRM member:

```text
member_id 37 -> extension 103
member_id 38 -> extension 104
```

The CRM sends `member_id` + `extension` with outbound requests. Do not let a browser choose another member identity.

## Recommended call activity mapping

When `call.started` arrives, create or reserve one call activity keyed by `call_id`.

When `call.answered` arrives:

- mark reached/connected true
- set `answered_at`

When `call.completed` arrives:

- set `ended_at`
- store duration
- finalize the activity

When `call.disposition` arrives:

- store disposition and notes
- do not overwrite factual duration/answer state from the carrier

## Idempotency

A webhook can be retried. Use `call_id` as the stable telephony identifier and store the latest lifecycle state. If the CRM has a separate activity primary key, add a unique external identifier such as `telephony_call_id`.

## Reports compatibility

Map the finalized telephony record into the same activity/log rows consumed by `crm_service.crm_analytics`. Do not add an alternative reporting calculation in this service. This preserves the existing rule that dashboard, reports and CSV use one calculation engine.

## Click-to-call UI

The CRM contact page should call the server-side CRM endpoint, which then calls this service:

```text
Browser -> EngineerIP -> Telephony API -> Asterisk
```

Do not make:

```text
Browser -> Asterisk ARI
```

and do not ship the master `TELEPHONY_TOKEN` to a browser.

## Incoming calls

For every explicitly configured DID, Asterisk now creates an inbound ARI call and signed lifecycle webhooks containing the caller in `phone`, the dialed DID in `caller_id_number`, owning `extension`, and `direction: inbound`. The CRM should normalize the caller number, look up a contact, and attach the inbound activity to that contact when found. Unknown callers can be stored as unmatched activities and linked later. Configure every production DID; the unmatched fallback route intentionally has no full CRM lifecycle.

## Authentication and API key management

Create a dedicated key in **Admin > API keys**. A normal CRM generally needs:

```text
config:read,calls:read,calls:write,recordings:read
```

Add voicemail scopes only if the CRM needs mailbox data. Do not grant `webhooks:manage` or `*` to normal application code. The generated `eip_...` secret is displayed once, stored only as SHA-256 in telephony, and can be revoked immediately from the panel.

The CRM server sends:

```http
Authorization: Bearer eip_<secret>
```

Never expose this key in browser JavaScript. Browser click-to-call must call the CRM backend; the backend authenticates to telephony.

## Recommended communication sequence

1. CRM backend calls `GET /api/v1/extensions` and `GET /api/v1/numbers?extension=101` to cache valid routing choices.
2. CRM backend calls `POST /api/v1/calls` with contact/member IDs, extension, destination, assigned caller-ID number, and a unique `Idempotency-Key` header.
3. Store the returned `call_id` as a unique external ID.
4. Telephony sends signed lifecycle webhooks to the CRM.
5. CRM verifies bearer token, timestamp and HMAC before processing.
6. CRM idempotently upserts state by `call_id`; never create duplicate activities for repeated events.
7. CRM may query `GET /api/v1/calls/<call_id>` to reconcile state.
8. Recording/voicemail audio should be proxied by the authenticated CRM backend, never fetched with an API key in browser code.

## Network and TLS

The preferred topology is a private Docker/VPC network. If telephony must be reachable across the Internet, place it behind HTTPS, an API gateway/reverse proxy, IP allowlisting and centralized rate limiting. ARI 8088 and AMI 5038 must remain private.
