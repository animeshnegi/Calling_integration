# Browser WebRTC

Asterisk WebRTC support uses PJSIP with a WSS transport. Modern browsers normally require a trusted HTTPS context for microphone/WebRTC use.

## Production topology

```text
EngineerIP CRM page
      |
      | authenticated short-lived browser capability
      v
Reverse proxy / TLS
      |
      | WSS
      v
Asterisk 8089
      |
      | SIP/RTP
      v
SIP trunk / phone endpoint
```

The ARI management API stays on the Docker network and is not exposed to the browser.

## Provisioning recommendation

Store WebRTC usernames/passwords against the CRM member/extension on the server side. Do not return the ARI master password or provider password to JavaScript.

A production browser session should obtain an expiring token or a narrowly scoped SIP credential from EngineerIP after normal CRM authentication. Revoke/rotate credentials when an employee is disabled.

## ICE / NAT

For remote clients behind NAT, configure STUN/TURN appropriate to your deployment. The example endpoint leaves `ice_servers` empty because the correct ICE infrastructure is environment-specific.

## Browser limitations

Microphone permission, HTTPS, firewall rules, NAT behavior, and SIP provider compatibility cannot be validated from a Git-only environment. These must be tested on the actual deployed hostname/network with a real SIP account.


## EngineerIP Phone PWA

The `feature/softphone-pwa` branch adds `/phone`, a responsive phone-style PWA using JsSIP. It provides a keypad, direct paste, recent calls, contacts, a Messages area, active-call controls, and an install button when the browser exposes the PWA install prompt.

The browser softphone connects directly to Asterisk over SIP WebSocket and WebRTC media. The Flask API is not in the audio path.

Asterisk renders a `transport-wss` PJSIP transport and makes the generated prefixed SIP username alias (for example `KUDGTE_101`) WebRTC-capable while leaving the canonical UDP/TCP extension endpoint unchanged for existing Zoiper and hardware phones. This follows Asterisk's WebRTC configuration model: a WSS transport plus an endpoint with `webrtc=yes`, which enables the required DTLS-SRTP, ICE, RTCP-mux and AVPF settings. See the official Asterisk WebRTC guidance.

Open:

```text
https://<your-service-host>/phone
```

The page is installable: it ships a manifest, a versioned service worker that
keeps the shell available offline and answers for the phone's own URLs only (it
never touches the console, the API or the CDN build of JsSIP), and it links the
EngineerIP logo as its favicon and iOS home-screen icon. Every other page (landing, sign-in,
console, documentation) links the same logo, and `/favicon.ico` answers with it
too, so no page ever falls back to a blank tab icon. Point a deployment at a
different image with `FAVICON_URL` if the brand moves.

For production, serve the page through trusted HTTPS and expose Asterisk WSS through the production reverse proxy. Do not place the ARI master credential or provider credentials in browser JavaScript.

Two things are needed before a browser can register, and neither is a code
change:

* A trusted certificate. `asterisk/entrypoint.sh` generates a self-signed pair
  in `/etc/asterisk/keys/` only so the listener can start; browsers reject it.
  Mount a real certificate and key there, or terminate TLS at the reverse proxy
  and forward the `/ws` path to the plain HTTP listener (`http://asterisk:8088/ws`).
  The softphone's default WebSocket URL is `wss://<sip domain>/ws`, which is the
  proxy path.
* A restart of the Asterisk container once, on a deployment that predates the
  WSS transport: transports are rendered into
  `dynamic/pjsip.transports.conf` and a `pjsip reload` does not add a new
  listener, so the `transport-wss` section only takes effect after Asterisk
  starts again. The phone itself needs no restart - it re-registers.
