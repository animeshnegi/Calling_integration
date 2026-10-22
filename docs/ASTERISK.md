# Asterisk Configuration

The container builds the pinned Asterisk source release on Debian Bookworm. At the time this repository was built, the upstream Asterisk downloads page lists 22.11.0 as the current 22.x release (27 Aug 2026). WebRTC support requires a PJSIP WebSocket transport and secure browser delivery in normal production use. See the upstream Asterisk WebRTC and ARI documentation for the version-specific module/configuration details.

## ARI

ARI is enabled on port 8088 inside the private Docker network. The integration
service reaches it as `http://asterisk:8088/ari`. Compose also publishes the HTTP
listener on **host loopback only** (`127.0.0.1:8088`) so host nginx can forward the
softphone's exact `/ws` path. Never proxy `/ari` or the full HTTP listener publicly;
use `deploy/nginx/softphone-websocket.conf`.

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

## Voicemail

`voicemail.conf` includes the database-rendered `dynamic/voicemail.dynamic.conf`. Enabled extensions receive a mailbox in the `engineerip` context, unanswered direct/DID calls route to `VoiceMail`, and `*97` opens `VoiceMailMain` for the calling extension. PJSIP mailbox association enables message-waiting indication on compatible phones. See [VOICEMAIL.md](VOICEMAIL.md).


## Exported-image CPU portability

The image is source-built from the pinned Asterisk release, not installed by apt.
Before compiling, `asterisk/Dockerfile` disables upstream `BUILD_NATIVE`
(`-march=native`) so workstation-built codec/core/pjproject objects can run on a
VM with a different CPU of the same target architecture. `KEEP_FRAME_POINTERS`
remains enabled for native crash diagnosis. The verified policy is recorded in
`/usr/share/asterisk/build-portability.txt`. Keep the requested Docker platform
consistent (e.g. linux/amd64); disabling CPU tuning does not convert architectures.
See OPERATIONS.md for the read-only extension-audio incident snapshot and required
live 101 ↔ 105 acceptance. A portable build fixes a concrete shipping defect; it
is not proof of the cause of an unclassified live restart.

Upstream source for the pinned release confirms the default and compiler flag:
[22.11.0 compiler choices](https://github.com/asterisk/asterisk/blob/22.11.0/build_tools/cflags.xml)
and [22.11.0 build rules](https://github.com/asterisk/asterisk/blob/22.11.0/Makefile.rules).
