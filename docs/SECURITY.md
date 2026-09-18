# Telephony Security Baseline

This project handles SIP credentials, phone numbers and a paid outbound calling capability. Asterisk's security guidance warns that incorrect authorization and dialplan configuration can permit unauthorized use and unexpected charges.

## Implemented controls

### API

- `/api/v1/*` requires either the emergency legacy master token or a revocable, hashed, least-privilege integration key. Scope checks return 403 before endpoint logic.
- Production rejects missing/weak API, Flask and ARI secrets.
- Bearer token comparison uses constant-time comparison.
- Outbound phone numbers must match E.164 syntax before reaching Asterisk.
- Requested extensions must be explicitly configured.
- Outbound caller IDs cannot be arbitrary: the number must be active and assigned to the originating extension. Its configured provider controls the trunk.
- Extension-user panel calls always override the requested extension with the account's server-side assignment.
- API request bodies are limited to 64 KiB.
- General and outbound-call rate limits reduce abuse and toll-fraud risk.
- Asterisk exception details are not returned to clients.
- Call IDs and disposition fields are validated and length-limited.
- Browser calling API and diagnostic UI are disabled by default.
- Security response headers are applied.
- Recording and voicemail audio is served only after bearer-token or admin-session authorization. Recording ARI access and the dedicated voicemail volume remain private.
- Voicemail PINs and the SendGrid API key are encrypted at rest and omitted from responses. PINs are restricted to 4–10 digits and rendered only into the private Asterisk configuration volume.
- Admin-panel RBAC scopes extension users to their assigned extension's calls, recordings and voicemail. System configuration, user administration, and SendGrid require the Administrator role.
- The last active administrator cannot be removed, and users cannot disable, demote, or delete their own administrator account.
- Voicemail email delivery is deduplicated with a persistent audio fingerprint and caps attachments at 20 MiB.
- Voicemail file operations validate mailbox/folder/message identifiers, reject symlinks and traversal, and preserve Asterisk message numbering.
- Webhook management requires a `webhooks:manage`/full API key or an authenticated administrator. Stored webhook/SIP secrets are encrypted at rest. Deliveries use bearer authentication plus timestamped HMAC-SHA256 signatures and unique delivery IDs for replay/idempotency controls. Webhook administrators are trusted: a configured URL causes a server-side outbound request, so limit admin access and use an outbound network policy where SSRF impact is a concern.

These controls address common OWASP API risks including broken authentication, unrestricted resource consumption, security misconfiguration and unsafe sensitive business flows.

### Asterisk

- ARI port 8088 is private to the Docker network and is not published.
- ARI WebSocket credentials are sent using an HTTP Authorization header rather than query-string credentials.
- IPComms inbound SIP is matched against the configured provider IP/CIDR allow-list.
- Local SIP endpoints require authentication.
- Unidentified SIP request thresholds are configured.
- SIP subscriptions are disabled unless an extension has voicemail enabled; voicemail endpoints permit subscriptions and are scoped to their own mailbox for message-waiting indication.
- Provider and local SIP legs use explicit codecs.
- RTP uses a defined port range.
- The current public Compose configuration exposes UDP 5060 and the RTP range only. WSS 8089 is kept unpublished until trusted TLS/reverse-proxy access is ready.
- The Asterisk runtime image removes most build-time packages after compilation.

Asterisk supports IP-based endpoint identification and ACL controls for PJSIP; provider source-IP matching is therefore retained for the IPComms trunk.

### Containers

- Flask runs as an unprivileged container user.
- Flask container drops Linux capabilities, uses `no-new-privileges`, a read-only root filesystem and a small `/tmp` tmpfs.
- Asterisk starts its runtime process under the `asterisk` account.
- No Docker socket is mounted.
- Secrets are supplied at deployment time rather than baked into images.

OWASP recommends unprivileged container users, capability reduction and careful secret handling, and recommends dedicated secret-management systems where practical.

## Required production actions

1. Generate unique random values for `SECRET_KEY`, `TELEPHONY_TOKEN`, `ASTERISK_ARI_PASSWORD`, `CRM_WEBHOOK_TOKEN`, all extension passwords and the IPComms password.
2. Do not print `.env`, `docker compose config` output containing secrets, or container environments into tickets/logs.
3. Rotate any credential that has previously been exposed in chat, terminal output, screenshots or logs.
4. Keep `.env` outside Git and restrict its filesystem permissions.
5. Keep GCP/UFW rules limited to UDP 5060 and the exact RTP range required by the deployment.
6. Do not publish Asterisk ARI 8088.
7. Do not expose WSS 8089 directly until a trusted certificate and deliberate reverse-proxy/TLS design are in place.
8. Put the Flask API behind the private CRM Docker network; if it must become externally reachable, add HTTPS, gateway authentication and centralized rate limiting first.
9. Run dependency/container vulnerability scans before production release.
10. Test outbound-call authorization and rate limits against the real CRM identity model before enabling click-to-call for multiple users.

## Remaining validation boundary

The outbound flow now persists deterministic channel IDs, rings the employee first, creates the customer leg only after employee answer, bridges both channels, and correlates recording events with the CRM call ID. Automated tests cover these state transitions. It is still not production-validated until carrier registration, live endpoints, NAT/RTP audio, inbound DID routing, recording playback, webhook behavior, firewall policy, and recovery are tested on the actual VM.
