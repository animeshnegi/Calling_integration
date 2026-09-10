# Asterisk Configuration

The container is based on Debian Bookworm's Asterisk package. At the time this repository was built, the upstream Asterisk downloads page lists 22.11.0 as the current 22.x release (27 Aug 2026). WebRTC support requires a PJSIP WebSocket transport and secure browser delivery in normal production use. See the upstream Asterisk WebRTC and ARI documentation for the version-specific module/configuration details.

## ARI

ARI is enabled on port 8088 inside the Docker network. It is **not published by `docker-compose.yml`**. The integration service reaches it as `http://asterisk:8088/ari`.

ARI credentials live in environment variables. Do not commit a real password to Git.

ARI has two pieces used by this project:

- REST requests for call control.
- WebSocket events for channel state changes and hangup notifications.

The service listens to `/ari/events` using the configured application name.

## SIP devices

The sample configuration contains extension `101`. Copy the same endpoint/AOR/auth pattern for additional employees (`102`, `103`, etc.). Each physical SIP phone or compatible softphone receives the matching username/password.

For remote devices, use TLS/SRTP or a VPN; do not expose a plain SIP endpoint unnecessarily. Strong per-user secrets are required.

## WebRTC

The sample `101-web` endpoint enables Asterisk WebRTC mode and a WSS transport. Production deployment should terminate trusted TLS with a certificate whose hostname matches the browser URL. The Asterisk documentation recommends trusted certificates such as Let's Encrypt because browsers generally reject self-signed certificates for this use case.

The sample web page is intentionally a **reference/diagnostic UI**. A production EngineerIP integration should provision per-member WebRTC credentials server-side and pass only short-lived/capability credentials to the browser.

## RTP

Default RTP range is `10000-10100/udp`. The VPS firewall must allow the same range from the SIP provider and appropriate client paths. A wider range may be required for larger deployments.

## Provider trunk

Replace the `provider` endpoint/auth/AOR/identify entries with your actual SIP provider's required transport, host, codecs, registration/contact syntax and authentication. Provider details vary; do not assume one generic config works for every carrier.

## Dialplan

`extensions.conf` has:

- Internal dialing for 1XX extensions.
- A provider route for E.164 numbers.
- A sample inbound route to extension 101.

For production, add provider-specific normalization and fraud controls, including maximum call duration, destination allowlists where practical, and per-member permissions.

## FreePBX

This repository uses **plain Asterisk configuration** rather than FreePBX so that the CRM integration owns the call-control contract. FreePBX can still be used as an administrative layer, but do not mix generated FreePBX configuration and hand-managed files in the same deployment without a clear ownership model.
