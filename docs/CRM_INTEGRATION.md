# Integrating your own systems

Telephony Control is a standalone service. It has no dependency on any
particular CRM, helpdesk or hosting panel, and it does not join a shared Docker
network with another product. Integration happens over HTTPS in two directions.

## The boundary

Keep your own application as the source of truth for contacts, members,
activity rows, outcomes and reporting. Telephony Control supplies factual call
lifecycle data only: what was dialled, whether it was answered, how long it
lasted, and the recording/voicemail artifacts it produced.

```
your app ──POST /api/v1/calls──> Telephony Control
your app <──signed webhook────── call.started / call.answered / call.completed …
```

## Inbound to telephony: the REST API

All `/api/v1/*` routes require `Authorization: Bearer <token>`. Create a
scoped API key per integration in the console (or sign up and create one for
your own tenant). Prefer scoped keys over the master `TELEPHONY_TOKEN` and
keep every token server-side. See [`API.md`](API.md) for the full contract,
including idempotency keys and the endpoint scope matrix.

Typical flow:

1. Your app stores the contact and decides to place a call.
2. It calls `POST /api/v1/calls` with an E.164 `phone` and an owned extension.
3. It records the returned `call_id` against its own activity row.
4. Later events and `GET /api/v1/calls/<call_id>` reconcile the outcome.

## Outbound to your app: signed webhooks

Register an endpoint in the console or via `POST /api/v1/webhooks` with a URL,
an encrypted bearer token, an active flag and an event subscription (`*` or a
comma-separated list). Deliveries are queued durably and retried.

- `Authorization: Bearer <endpoint token>`
- `X-EngineerIP-Signature: sha256=<HMAC-SHA256 of timestamp + "." + raw body>`
- `X-EngineerIP-Timestamp`, `X-EngineerIP-Delivery`

Verify the signature over the exact raw bytes before parsing JSON. See
[`WEBHOOKS.md`](WEBHOOKS.md) and [`CRM_SECURITY_AUDIT.md`](CRM_SECURITY_AUDIT.md)
for the verification snippet, scope matrix and production checklist.

## Deployment

Put the API behind an HTTPS reverse proxy (`127.0.0.1:5000` is published on
loopback by Compose). Point your integration at that public base URL. No
`docker network connect`, shared bridge or panel-specific network is required.

## Notes

- Never expose ARI 8088 or AMI 5038 to the Internet.
- Never put `TELEPHONY_TOKEN` in a browser; proxy the API server-side.
- Webhook secrets and API keys are encrypted at rest and are tenant-scoped.
- Playback URLs are authenticated; proxy them rather than embedding a master
  token in an `<audio>` element.
