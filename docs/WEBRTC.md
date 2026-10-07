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

## Answering in the browser

The two endpoints hold two media modes, and the endpoint a call is placed
towards is what decides the media it is set up with - so a browser and a
hardware phone cannot share one:

| Device | Endpoint | Media |
| --- | --- | --- |
| Hardware phone, Zoiper, desk phone | `PJSIP/<extension>` (UDP/TCP) | plain RTP, G.722 first |
| Browser PWA | `PJSIP/<generated username>` (WSS, `webrtc=yes`) | DTLS-SRTP, ICE, RTCP-mux |

A browser refuses an unencrypted RTP offer, and a hardware phone refuses a
DTLS-SRTP offer (`UDP/TLS/RTP/SAVPF`). So calls are dialled towards the endpoint
that fits the device that answers them:

* An extension ticked **Answers in the browser (WebRTC)** is dialled on its
  WebRTC endpoint - dialling extension, ringing a group, an IVR selection, a
  call flow and the console's click-to-call all follow it. The extension card
  then shows a *Browser phone* tag.
* Every other extension is dialled on its plain endpoint, which is what the
  credentials in the sheet are written for.
* Anything else - a customer calling another customer's number, or an outside
  caller - arrives from the carrier and the number's own call flow decides.

Give a browser extension its own extension number rather than sharing one with a
desk phone: the switch belongs to the extension, so a plain phone registered on
a WebRTC-enabled extension is offered media it cannot accept.

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

## HD voice

A call is "HD" when the audio is wideband - 16 kHz, roughly 7 kHz of speech
instead of the 3.4 kHz a normal phone line carries. What this platform does
about it, and what it cannot:

* Every device the platform provisions - desk phone, softphone or the browser -
  is offered the same codecs, in this order: **G.722, then PCMU, then PCMA**
  (`TelephonyConfigSync.INTERNAL_CODECS`). G.722 is wideband and is built into
  the Asterisk image, so a call between two devices that support it is HD end to
  end and is not transcoded. PCMU/PCMA stay behind it so a device that cannot do
  wideband gets a normal call instead of a failed one, and chan_pjsip's
  `incoming_call_offer_pref` default (`local`) keeps this order when the far end
  offers its own.
* The browser phone never claims more than it negotiated: the call screen shows
  what the live WebRTC stats say - "HD · G722" or "Standard · PCMU" - and adds
  "· unstable network" when packet loss or jitter crosses the threshold. SIP
  does not renegotiate a codec by itself, so the badge reports the call instead
  of promising one.
* Toward a carrier, HD depends on the carrier. A call to a PSTN number is only
  wideband if the SIP trunk carries G.722: put `g722,ulaw,alaw` in the provider's
  Codecs field (console → Providers) after confirming the carrier supports it. A
  trunk that only does G.711 is better left on `ulaw,alaw` - the internal leg
  would then be transcoded for no gain in the audio the caller hears.
* Opus is not available in this image. Asterisk ships the Opus codec separately
  from its core tarball, so `codec_opus` would have to be built and `libopus0`
  kept at runtime before a browser's Opus offer could be accepted. G.722 is the
  wideband codec that is always present, and it is what the badge reports.

The browser captures the microphone with its own echo cancellation, noise
suppression and automatic gain, which is what keeps a laptop or handset call
usable without a headset.
