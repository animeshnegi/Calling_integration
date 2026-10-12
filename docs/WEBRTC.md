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

Asterisk renders a `transport-wss` PJSIP transport and, **only for extensions whose WebRTC switch is on**, a second WebRTC-capable endpoint under the extension's generated technical SIP username (for example `MERIDIAN_101_13025550001`) while leaving the canonical UDP/TCP endpoint - and the SIP registration hardware phones already use - unchanged. The alias exists only when `webrtc_enabled = true`: **checking WebRTC never disables normal SIP.** This follows Asterisk's WebRTC configuration model: a WSS transport plus an endpoint with `webrtc=yes`, which enables the required DTLS-SRTP, ICE, RTCP-mux and AVPF settings. See the official Asterisk WebRTC guidance.

### Opening the softphone from the console

Each extension card in the customer console has a **Softphone** button. It opens `/phone?connect=extension` in a new window, signed in as *that* extension - the card's own key (`101@+13025550002`) names the identity, so two 101s on two numbers open two different softphones. The page reports ready to the console that opened it; the console answers with the extension's SIP username, password, domain and WebSocket address over `postMessage`, checking origin and source on both sides. The password never appears in a URL or in server storage, and the new window keeps the sign-in in memory only: a phone session saved in the same browser is not picked up.

The window opens at phone size (390 × 860, centred, no toolbars). The page lays itself out to whatever size the window is: the keypad rows shrink with the height, the call button and the navigation always stay on screen, and only the lists scroll. On a phone, or in a window no wider than 520 px, the phone fills the whole window.

The Softphone button needs **Browser phone** (`webrtc_enabled`) on that extension. Without it the browser endpoint does not exist, so the console says so instead of opening a phone that cannot register.
It also needs the platform's **service host** set in Settings, because the phone registers with that address. Without it the console closes the window and says so, rather than handing over a server the browser cannot reach.

## Answering in the browser

The two endpoints hold two media modes, and the endpoint a call is placed
towards is what decides the media it is set up with - so a browser and a
hardware phone cannot share one:

| Device | Endpoint | Media |
| --- | --- | --- |
| Hardware phone, Zoiper, desk phone | `PJSIP/101-13025550001` (UDP/TCP) | plain RTP, G.722 first |
| Browser PWA (switch on) | `PJSIP/MERIDIAN_101_13025550001` (WSS, `webrtc=yes`) | DTLS-SRTP, ICE, RTCP-mux |

**A three-digit extension is resolved only within the current phone number**, so
the endpoint name always carries both halves: `101-13025550001` is *101 on
+13025550001*. A PJSIP section name cannot contain `@` (Asterisk reads it as a
key/value pair), and the digits alone would be ambiguous the moment two lines
hold 101 - `PJSIP/101` does not exist. The store's `endpoint_name()` and the
renderer's `_section_name()` decide the same name from the same rule. Only the
platform's own rows - devices with no number - keep their bare digits, because
they answer in the platform's single context. SIP usernames are technical
identifiers that devices register with; **no person ever dials one**.

A browser refuses an unencrypted RTP offer, and a hardware phone refuses a
DTLS-SRTP offer (`UDP/TLS/RTP/SAVPF`). So calls are dialled towards the endpoint
that fits the device that answers them:

* An extension ticked **Answers in the browser (WebRTC)** is dialled on its
  WebRTC endpoint - dialling extension, ringing a group, an IVR selection, a
  call flow and the console's click-to-call all follow it. The extension card
  then shows a *Browser phone* tag. Its plain endpoint is still rendered, so the
  SIP device keeps working and the tick can be removed again at any time.
* Every other extension is dialled on its plain endpoint, which is what the
  credentials in the sheet are written for.
* A full number the platform owns - of the same customer or of another one - is
  reached inside the platform, and rings that number's own inbound destination,
  so a browser calling it reaches the WebRTC endpoint of that destination. Only
  numbers the platform does not own, and outside callers, arrive from the
  carrier. A three-digit extension is resolved only within the current phone
  number, and `<number>*<digits>` reaches another of the same customer's
  extensions; another customer's extensions are not dialable.

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
