# CRM API and Webhook Security Audit

## Result

The service now has the application-level controls required for a private CRM integration: revocable scoped API keys, request validation, extension/DID ownership checks, outbound-call idempotency, persistent signed webhook delivery, admin RBAC, encrypted secrets, and private ARI/AMI. Production security still depends on HTTPS/private networking, firewalling, backup, monitoring, and carrier validation described below.

## CRM authentication

Create a key in **Admin > API keys**. The secret starts with `eip_`, is shown once, and is stored only as a SHA-256 hash. The panel shows name, non-secret prefix, scopes, creation time, last use, active/revoked state, and a revoke action.

Recommended normal CRM scopes:

```text
config:read,calls:read,calls:write,recordings:read
```

Optional scopes:

- `voicemail:read` — list/play voicemail;
- `voicemail:write` — mark/delete voicemail;
- `webhooks:manage` — change delivery destinations; grant only to deployment automation;
- `*` — full integration access; avoid for normal CRM runtime.

The legacy `TELEPHONY_TOKEN` remains an emergency bootstrap/master key stored in VM environment configuration. It cannot be viewed in the panel. Use scoped keys for integrations and rotate/revoke them independently.

## Endpoint authorization matrix

| Function | Scope |
|---|---|
| Extensions, numbers, providers | `config:read` |
| List/get calls | `calls:read` |
| Start, hang up, disposition | `calls:write` |
| List/play recordings | `recordings:read` |
| List/play voicemail | `voicemail:read` |
| Mark/delete voicemail | `voicemail:write` |
| Manage/test webhooks | `webhooks:manage` |
| Test-only ARI event intake | full `*` key or legacy master |

Missing/invalid/revoked keys receive 401. Valid keys without the required scope receive 403. Keys are rate-limited and `last_used_at` is updated.

## Duplicate-call protection

CRM should send a unique header for every intended call:

```http
Idempotency-Key: crm-job-550e8400-e29b-41d4-a716-446655440000
```

Keys are isolated by API client and retained for 24 hours. A repeated completed request returns the original call rather than originating another paid call. A simultaneous duplicate returns 409.

## Webhook authentication

A webhook secret is encrypted in the settings database and used for both bearer authentication and body signing. Every request includes:

```http
Authorization: Bearer <webhook secret>
X-EngineerIP-Delivery: <stable UUID>
X-EngineerIP-Timestamp: <Unix timestamp>
X-EngineerIP-Signature: sha256=<HMAC hex>
```

CRM verification order:

1. Read the raw request body bytes.
2. Reject timestamps more than five minutes old.
3. Compute `HMAC-SHA256(secret, timestamp + "." + raw_body)`.
4. Constant-time compare against `X-EngineerIP-Signature`.
5. Authenticate the bearer secret as an additional check.
6. Insert `X-EngineerIP-Delivery` into a uniquely indexed receipt table; duplicates return 2xx without processing twice.
7. Parse JSON and idempotently upsert by `call.call_id`.
8. Return 2xx only after committing the CRM transaction.

## Delivery durability

Admin-managed lifecycle events are committed to the durable database outbox before network delivery. The dedicated worker sends queued events every few seconds and retries failures with backoff up to five attempts. The delivery UUID remains stable across retries. Completed/terminal delivery records are retained for 30 days and visible in admin state.

The old environment-only `CRM_WEBHOOK_URL` fallback is best-effort. Create that endpoint in **Admin > Webhooks** before production to use the durable outbox.

## Inbound CRM communication

Every explicitly configured DID enters ARI and creates an inbound call record. Signed events identify:

- `direction: inbound`;
- external caller in `phone`;
- dialed/owned DID in `caller_id_number`;
- owning employee in `extension`.

Only the owning extension is originated. Answer, bridge, completion/failure, recording, and voicemail-handoff events use the same contract as outbound calls. The unmatched `s` fallback rings one configured fallback extension but does not have the complete CRM lifecycle; every production DID must be explicitly configured.

## Admin and user panel controls

Administrators manage API keys, webhook URLs/secrets/event filters, SendGrid, users, extensions, DIDs, providers and global settings. Extension users are server-side scoped to their own calls, recordings, voicemail and assigned callback numbers. UI hiding is not relied upon for authorization.

Admin sessions use HttpOnly, SameSite=Lax cookies, Secure cookies in production, CSRF tokens on mutations, scrypt password hashes and login rate limits. Disabled users are invalidated on their next request.

## Data and secret protection

- API key secrets: one-time display; SHA-256 hash at rest.
- SIP, provider, webhook and SendGrid secrets: Fernet encryption derived from `SECRET_KEY`.
- Passwords: scrypt hashes.
- ARI/AMI: private Docker network; no host publication.
- Recording access: authenticated proxy through private ARI.
- Voicemail access: authenticated, path-validated private volume.
- Caller ID: must be active, provider-bound and owned by originating extension.

Changing/loss of `SECRET_KEY` makes encrypted secrets unreadable. Store it in a proper VM secret manager and back it up securely.

## Required infrastructure controls

Application controls do not replace infrastructure security:

1. Keep the API on a private VPC/Docker network where possible.
2. If externally exposed, require HTTPS with a trusted certificate and place it behind a reverse proxy/API gateway.
3. Add gateway-wide rate limiting because Flask's built-in limiter is per process.
4. IP-allowlist the CRM and administrator networks where practical.
5. Never expose ARI 8088 or AMI 5038.
6. Restrict SIP/provider IP ranges and RTP firewall ports.
7. Monitor API 401/403/429, webhook failures, SendGrid errors, disk usage and toll activity.
8. Rotate integration/webhook keys periodically and immediately after exposure.
9. Back up `telephony_data`, recordings and voicemail volumes.
10. Test restore, key revocation, duplicate-call handling, webhook replay rejection and carrier callback routing before production.

## Intentional limitations

- Centralized/distributed rate limiting requires the production gateway.
- Webhook retries stop after five failed attempts; monitor terminal failures and provide an operational replay procedure if required.
- The service does not provide public OAuth/OIDC. CRM-to-service authentication uses high-entropy scoped bearer keys over a trusted TLS/private channel.
- Carrier caller-ID presentation and inbound Request-URI format must be validated with the live provider.
