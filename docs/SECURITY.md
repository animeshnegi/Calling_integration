# Telephony Security Baseline

This project handles SIP credentials, phone numbers and a paid outbound calling capability. Asterisk's security guidance warns that incorrect authorization and dialplan configuration can permit unauthorized use and unexpected charges.

## Implemented controls

### API

- Master API token is required for `/api/v1/*`.
- Production rejects missing/weak API, Flask and ARI secrets.
- Bearer token comparison uses constant-time comparison.
- Outbound phone numbers must match E.164 syntax before reaching Asterisk.
- Requested extensions must be explicitly configured.
- API request bodies are limited to 64 KiB.
- General and outbound-call rate limits reduce abuse and toll-fraud risk.
- Asterisk exception details are not returned to clients.
- Call IDs and disposition fields are validated and length-limited.
- Browser calling API and diagnostic UI are disabled by default.
- Security response headers are applied.

These controls address common OWASP API risks including broken authentication, unrestricted resource consumption, security misconfiguration and unsafe sensitive business flows.

### Asterisk

- ARI port 8088 is private to the Docker network and is not published.
- ARI WebSocket credentials are sent using an HTTP Authorization header rather than query-string credentials.
- IPComms inbound SIP is matched against the configured provider IP/CIDR allow-list.
- Local SIP endpoints require authentication.
- Unidentified SIP request thresholds are configured.
- SIP subscriptions are disabled for the current endpoint design.
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

## Important remaining design item

The current API's outbound call-originator is still a POC. The final production flow should make Asterisk ring the selected employee extension first, wait for the employee to answer, originate the customer leg, bridge the two channels, and correlate both channel IDs to one CRM call ID. That change should be completed and tested before treating the API as production-ready.
