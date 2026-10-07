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

For production, serve the page through trusted HTTPS and expose Asterisk WSS through the production reverse proxy. Do not place the ARI master credential or provider credentials in browser JavaScript.
