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

Each extension card in the customer console has a **Softphone** button. It opens `/phone?connect=extension` in a new window, signed in as *that* extension - the card's own key (`101@+13025550002`) names the identity, so two 101s on two numbers open two different softphones. The page reports ready to the console that opened it; the console answers with the extension's browser SIP username (its own `-webrtc` identity), password, domain and WebSocket address over `postMessage`, checking origin and source on both sides. The password never appears in a URL or in server storage, and the new window keeps the sign-in in memory only: a phone session saved in the same browser is not picked up.

The window opens at phone size (390 × 860, centred, no toolbars). The page lays itself out to whatever size the window is: the keypad rows shrink with the height, the call button and the navigation always stay on screen, and only the lists scroll. On a phone, or in a window no wider than 520 px, the phone fills the whole window.

The Softphone button is shown only while **Browser phone** (`webrtc_enabled`) is on for that extension - without it the browser identity and endpoint do not exist, so the button is hidden for users and administrators alike rather than opening a phone that cannot register.
It also needs the platform's **service host** set in Settings, because the phone registers with that address. Without it the console closes the window and says so, rather than handing over a server the browser cannot reach.

## Answering in the browser

A browser endpoint renders `from_domain` from the configured SIP Service
address. When Asterisk sends a call to that endpoint, it uses a valid SIP host
for the dialog's initial From and Contact instead of falling back to a Docker
container ID such as `123abc456def`. JsSIP 3.10.1 rejects that numeric-leading,
**single-label** host in either header before sending 100/180 or showing an
incoming call. This setting applies only to browser endpoints; plain SIP and
carrier From domains are unchanged. The configuration worker reloads PJSIP
when the endpoint is updated. Check the live endpoint and a fresh call after
release; the file alone is not proof that a running Asterisk loaded it.

The browser plays an audible local ringtone while the incoming call is pending
and stops it on answer, decline, cancellation or logout. Browsers can block
sound before a gesture in a newly opened window: use **Enable incoming call
sound** on the phone if it appears. When the phone was opened from the console,
the console also provides a same-origin, gesture-unlocked ringing fallback
while the phone window is blocked; keep the console open for that fallback.
The browser and Windows must both allow sound, the chosen Windows default
output must be unmuted, and OS/browser policies can still silence a background
window. The ringtone is local and never changes call RTP.

When the PBX reports ringing without usable early audio, the caller hears local
ringback. A carrier's 183 early media is left alone so announcements are not
covered; when a 180 contains SDP but no decoded audio, ringback fills the
silence and stops as soon as real audio arrives. Answer, failure and hangup
also stop it. Sound plays on the Windows/browser default output; the call menu
no longer overrides the output device.

Asterisk plays the packaged instrumental loop to held plain SIP/carrier legs.
Browser endpoints instead pass hold/unhold in SIP re-INVITEs; the held browser
shows **The other person put you on hold** and plays the same loop locally.
Resume stops the loop and restores call audio. A third-party phone's own display
is controlled by that phone: it may hear Asterisk's music without showing a
hold badge. Both API and Asterisk images must be updated, and all softphone
windows must be reopened to use the new browser hold handling.

The two endpoints hold two media modes, and the endpoint a call is placed
towards is what decides the media it is set up with - so a browser and a
hardware phone never share one identity, one endpoint or one contact space:

| Device | Sign-in (SIP username) | Endpoint | Media |
| --- | --- | --- | --- |
| Hardware phone, Zoiper, desk phone | `MERIDIAN_101_13025550001` | `PJSIP/101-13025550001` (UDP/TCP) | plain RTP, G.722 first |
| Browser PWA (switch on) | `MERIDIAN_101_13025550001-webrtc` | `PJSIP/MERIDIAN_101_13025550001-webrtc` (WSS, `webrtc=yes`) | DTLS-SRTP, ICE, RTCP-mux |

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
DTLS-SRTP offer (`UDP/TLS/RTP/SAVPF`). Each device kind therefore registers on
its own SIP identity - the browser's carries `-webrtc`, with the same password -
and PJSIP answers it on the endpoint named after it. A Zoiper signing in with
the plain username is matched to the plain endpoint even when the WebRTC switch
is on; before this separation it was matched to the WebRTC endpoint and every
call carried media it could not speak: connected, billed, and silent.

Calls then ring **both** endpoints of a WebRTC-ticked extension in one parallel
Dial - one leg per device kind, each with the media that device speaks - and the
first to answer takes the call:

* An extension ticked **Answers in the browser (WebRTC)** carries a second,
  browser-only identity beside its plain one - dialling extension, ringing a
  group, an IVR selection, a call flow and the console's click-to-call all ring
  both. The extension card then shows a *Browser phone* tag, and the credentials
  sheet lists the browser username beside the plain one.
* Every extension is also dialled on its plain endpoint, which is what the
  credentials in the sheet are written for, so a desk phone or softphone is
  never disabled by the switch.
* A full number the platform owns - of the same customer or of another one - is
  reached inside the platform, and rings that number's own inbound destination
  on every endpoint of that destination. Only numbers the platform does not
  own, and outside callers, arrive from the carrier. A three-digit extension is
  resolved only within the current phone number, and `<number>*<digits>` reaches
  another of the same customer's extensions; another customer's extensions are
  not dialable.

One extension number can serve a desk phone and a browser side by side: the
switch adds the browser identity, never touches the plain one, and the console's
**Softphone** button appears only while the switch is on. Switching WebRTC off
removes the browser identity - Asterisk drops it through a restart that
`apply()` performs automatically, because a reloaded-out endpoint can linger in
memory.

Open:

```text
https://<your-service-host>/phone
```

The page is installable: it ships a manifest, a versioned service worker that
keeps the shell available offline and answers for the phone's own URLs only (it
never touches the console or the API), and it links the
EngineerIP logo as its favicon and iOS home-screen icon. Every other page (landing, sign-in,
console, documentation) links the same logo, and `/favicon.ico` answers with it
too, so no page ever falls back to a blank tab icon. Point a deployment at a
different image with `FAVICON_URL` if the brand moves.

The SIP stack is vendored: the JsSIP browser build ships with the app at
`/jssip.min.js` (same origin, precached in the offline shell), so opening the
softphone never depends on a third-party CDN. The page falls back to the pinned
upstream build (`jssip@3.10.1`) only if that copy cannot be fetched.

For production, serve the page through trusted HTTPS and expose Asterisk WSS through the production reverse proxy. Do not place the ARI master credential or provider credentials in browser JavaScript.

Two things are needed before a browser can register, and neither is a code
change:

* A trusted certificate. `asterisk/entrypoint.sh` generates a self-signed pair
  in `/etc/asterisk/keys/` only so the listener can start; browsers reject it.
  Mount a real certificate and key there, or terminate TLS at the reverse proxy
  and forward the `/ws` path to the plain HTTP listener (`http://asterisk:8088/ws`).
  The softphone's default WebSocket URL is `wss://<web host serving the phone>/ws`,
  not the SIP domain. Install/include `deploy/nginx/softphone-websocket.conf` in
  that host's active HTTPS vhost (see DEPLOYMENT.md), then run
  `python3 scripts/check-softphone-websocket.py wss://YOUR_WEB_HOST/ws`.
  The Flask app cannot serve `/ws` or install routes into host nginx. For a
  1Panel/OpenResty container, use the container-aware installer in DEPLOYMENT.md:
  `sudo python3 scripts/install-softphone-proxy.py --domain YOUR_WEB_HOST`
  (dry run first). A bridged proxy must use a shared Docker network, not its own
  loopback address for Asterisk.
* A restart of the Asterisk container once, on a deployment that predates the
  WSS transport: transports are rendered into
  `dynamic/pjsip.transports.conf` and a `pjsip reload` does not add a new
  listener, so the `transport-wss` section only takes effect after Asterisk
  starts again. The phone itself needs no restart - it re-registers.

If calls fail at media setup and Asterisk logs `SRTP support module is not
loaded or available. Try loading res_srtp.so.` followed by `Couldn't negotiate
stream ... (nothing)`, the image was built without libsrtp2 and contains no
`res_srtp.so` at all (`asterisk -rx "module show like res_srtp"` lists nothing).
A `webrtc=yes` endpoint always negotiates DTLS-SRTP, so every browser call dies
exactly there while registration keeps working. No reload or config change can
add a module that was never compiled: rebuild the Asterisk image -
`asterisk/Dockerfile` compiles with `libsrtp2-dev`, keeps `libsrtp2-1` at
runtime, and refuses to finish the build unless `res_srtp.so` was produced -
then recreate the container. The entrypoint also warns at startup when the
module is missing, so an old image cannot fail quietly.

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

## Why an unregistered parallel leg logged `invalid URI 'section,username'`

A live log may show a WebRTC endpoint ringing normally while the plain parallel
leg fails like this:

```text
Dial("PJSIP/ENGINEERIP_102_...-webrtc&PJSIP/102-...,30")
ERROR: Endpoint '102-...': Could not create dialog to invalid URI
       '102-...,ENGINEERIP_102_...'.  Is endpoint registered and reachable?
Called PJSIP/ENGINEERIP_102_...-webrtc
No one is available to answer at this time
```

The comma-separated text is the endpoint's **AOR list**, not a SIP URI. In
Asterisk 22, `ast_sip_session_create_outgoing()` asks
`ast_sip_location_retrieve_contact_and_aor_from_list_filtered()` for a reachable
contact; when none is registered it falls back to `endpoint->aors` as the
outgoing URI. A direct `Dial(PJSIP/section)` therefore hands PJSIP a bare AOR
list when the desk phone is offline. See the pinned upstream sources:
[`res/res_pjsip_session.c`](https://github.com/asterisk/asterisk/blob/22.11.0/res/res_pjsip_session.c),
[`res/res_pjsip/location.c`](https://github.com/asterisk/asterisk/blob/22.11.0/res/res_pjsip/location.c)
and [`channels/pjsip/dialplan_functions.c`](https://github.com/asterisk/asterisk/blob/22.11.0/channels/pjsip/dialplan_functions.c).

The dialplan now uses Asterisk's `PJSIP_DIAL_CONTACTS()` function for every
extension endpoint:

```text
Dial(${PJSIP_DIAL_CONTACTS(<browser-endpoint>)}&${PJSIP_DIAL_CONTACTS(<plain-endpoint>)},30)
```

That function expands only **reachable contact URIs**. An unregistered device
expands to an empty string; `app_dial.c` ignores empty `&` positions, so the
other device still rings in the same one parallel Dial. If no device is
registered at all, Dial sets `CHANUNAVAIL` and the existing voicemail/no-service
step runs without an invalid URI. This also preserves the browser/plain media
separation: each contact is dialled through its own endpoint, so a browser still
gets DTLS-SRTP and a desk phone still gets plain RTP. Outbound trunk Dial lines
are unchanged.

## Browser dial/answer: SIP setup and Docker ICE

A registration and a ringing browser do **not** prove the call was answered or
that media works. In the reported trace the desk-phone leg rang the browser for
30 seconds and went to voicemail: Asterisk did not log an answer/bridge. The
trace does not prove why the answer was missing, or whether an outbound browser
INVITE was sent. Keep signalling and RTP failures separate when investigating.

JsSIP 3.10.1 waits for local ICE gathering before sending an initial INVITE or
an incoming 200 OK. With STUN-only settings, a reflexive candidate triggers a
short 250 ms settling window; if only host candidates arrive, the fallback is
four seconds. When TURN is configured, the phone waits for a relay candidate
before the quick release and allows up to eight seconds for it. Normal
completion proceeds immediately. A slow STUN server or TURN relay may still
arrive after its fallback, so this is not proof a usable candidate was gathered;
verify restrictive networks and use a working TURN server where required. No
candidate is ever invented. If there are no candidates (or microphone
permission remains pending), the phone reports a setup timeout instead of
spinning forever. Answer is marked and timed only on SIP acceptance; ICE
checking before then is displayed as preparation, not connected audio.

Asterisk 22's PJSIP `external_media_address` can rewrite the SDP `c=` address
when its NAT rules treat the signalling destination as external; a local reverse
proxy may leave `c=` private. It **does not rewrite ICE host candidates**. A
private `c=` by itself does not prove failure when ICE selects a reachable
candidate. For the supplied Compose topology
(one Docker bridge, IPv4 public address, and identical external/internal RTP
ports), the Asterisk entrypoint records its actual container IPv4 address and
creates `/etc/asterisk/dynamic/rtp.ice.conf`. The API sync maps it to the
validated numeric Service address using `[ice_host_candidates]`. Both images
are required. The mapping is rebuilt at Asterisk container startup (so a new
bridge IP is not hardcoded), refreshed on settings changes, and the RTP module
is reloaded without unloading it or restarting existing calls. API and worker
writes share a lock; pending reloads are retried. The last validated public IP
is kept in the shared dynamic volume for the next Asterisk boot. An unresolved,
private, special or Cloudflare-proxied Service address is rejected by the sync
and the last valid configuration is kept. TEST-NET documentation addresses are
accepted for examples/tests only and cannot carry real calls.

This automatic host mapping is for **static one-to-one NAT with unchanged RTP
ports**, as in the supplied Compose file. It is not a general solution for
port-remapping NAT, multi-homed or IPv6-only deployments; these need a separately
validated ICE/network configuration. A TURN server may still be necessary for
restrictive browser networks. UDP 10000–10100 (or the configured RTP range)
must really reach Asterisk; loading images or seeing a public SDP address does
not prove the firewall path.

After deploying both images, inspect the generated mapping without printing
credentials:

```bash
docker exec engineerip-asterisk sh -c 'cat /etc/asterisk/dynamic/rtp.ice.conf'
docker exec engineerip-asterisk asterisk -rx 'rtp show settings'
docker compose logs --tail=80 telephony-ari asterisk
```

The include should map the **actual** Docker IPv4 to the VM's numeric public
media IP. A missing mapping, a boot warning, or a pending RTP reload needs
attention; a correct file alone does not prove the RTP engine loaded it or that
packets can traverse the network. With SIP logging enabled briefly, inspect the
actual `a=candidate` lines of a test call locally and disable logging afterward;
SIP logs can contain telephone numbers and credentials. Finally call in both
directions from the real devices and confirm two-way audio.

Local regression includes two real headless Chromium peers, the vendored JsSIP,
SIP/WebSocket signalling, actual bidirectional encrypted RTP packet counters
and decoded non-silent audio energy, deliberately stalled gathering,
denial/SDP-failure paths, and Asterisk config
lifecycle/AMI tests. The signalling relay is a test double, not Asterisk, and
none of these checks proves the deployed VM, carrier or Internet NAT path.

## Silent calls, two-minute drop-outs, and call-screen responsiveness

A browser call that rings but has no audio can have several separate
faults: the remote WebRTC track was never attached to an `<audio>` element, the
browser blocked autoplay, or ICE advertised an unreachable media address. The
softphone now fixes the first two in code and reports the third instead of
leaving the call screen silent:

* `web/phone.js` binds the remote track handler both to `session.connection` and
  to the `peerconnection` event. JsSIP creates the peer connection and emits
  that event **synchronously inside `ua.call()`**, before `attachSession()` can
  subscribe; subscribing only to the event left outbound calls with no remote
  audio element. The handler accepts `event.streams[0]`, a bare `event.track`
  (no `a=msid` stream), and tracks already present in `pc.getReceivers()`. It
  uses `addEventListener("track", …)` where available and never overwrites an
  existing handler.
* The `<audio autoplay playsinline>` element is primed inside the Call and
  Answer clicks. If the browser still rejects `play()`, an in-call
  **Tap to enable call audio** button appears; any later tap retries playback.
  `remoteAudio.muted` is never used as a fallback, so the far end cannot be
  accidentally silenced. A denied `getUserMedia()` ends the call with
  “Microphone unavailable” instead of leaving a dead connected screen.
* SIP **session timers are enabled** with `UPDATE` refresh, registration expiry
  is 300 seconds, and a REGISTER keepalive runs every 30 seconds. The last one
  is important behind proxies/CDNs that close an idle WebSocket after roughly
  100–120 seconds: without signalling traffic, a phone that worked at first can
  no longer make or receive calls until the page is reloaded. WebSocket
  recovery is bounded to 2–8 seconds, with a watchdog on `online` and tab
  `visibilitychange` for browsers that suspended timers.
* The connection badge now has a distinct **Reconnecting…** state instead of
  showing Offline with no explanation. The in-call status follows ICE state
  (`Connecting audio…`, `Connected`, `Network interrupted…`, `Media connection
  failed`). An in-call DTMF keypad and physical-keyboard DTMF support IVR
  navigation without leaving the call screen.
* `phone.html` requests versioned `/phone.css?v=N` and `/phone.js?v=N`, and
  `sw.js` precaches exactly those URLs under `eip-phone-vN`. A browser that
  cached the previous `public, max-age=3600` script under the unversioned URL
  cannot answer the new page with stale call code. Bump both the HTML version
  and the service worker `VERSION` together on future shell changes.

These are browser fixes; they do not change outbound call routing. The optional
`tools/phone-asteriskcheck.js` regression starts a disposable local Asterisk,
real browser sessions and a plain SIP client using generated test credentials;
its prerequisites and invocation are at the top of that script. It tests real
loopback signalling and audio, not the production VM or network. A live
deployment still needs the checks that only the VM can prove:

```bash
python3 scripts/check-softphone-websocket.py ws://127.0.0.1:8088/ws
python3 scripts/check-softphone-websocket.py wss://YOUR_WEB_HOST/ws
docker compose exec asterisk asterisk -rx 'pjsip show contacts'
docker compose exec asterisk asterisk -rx 'module show like res_srtp'
```

The admin **Service address** must resolve to the VM's public numeric IP; do
not point it at a Cloudflare-proxied hostname. UDP 10000–10100 must reach the
Asterisk container. If STUN-only ICE still produces silence on a restrictive
office network, configure a TURN server in the platform's ICE settings so the
browser has a relay candidate. The call-screen quality badge reports the
negotiated codec and unstable packet loss/jitter; it cannot replace a live RTP
capture when the network path itself is broken.

## Calls back to a Zoiper / desk phone, and `.env`

A log that starts with `PJSIP/AUMPNO_101` on UDP and dials extension `102` is
**Zoiper → browser**, not browser → Zoiper. In that direction the log may show
only the `102-webrtc` contact, which is correct when `102` is a browser-only
extension: `PJSIP_DIAL_CONTACTS()` skips the unregistered plain leg. The failing
direction needs its own log, plus:

```bash
docker compose exec asterisk asterisk -rx 'pjsip show contacts'
docker compose exec asterisk asterisk -rx 'pjsip show aors'
docker compose exec asterisk asterisk -rx 'pjsip show endpoint 101-13022661626'
```

The plain device's contact must be **Reachable**. Dynamic AORs for plain SIP
devices now carry `qualify_frequency=30`, `qualify_timeout=3`,
`authenticate_qualify=no` and `remove_unavailable=yes`. Asterisk sends an
unauthenticated OPTIONS every 30 seconds, which keeps the phone's NAT/UDP
mapping alive and marks a phone that stopped answering as unreachable. This is
the common reason a Zoiper can place calls (its INVITE refreshes NAT) but does
not ring when the browser calls back (the stored contact has gone stale). The
browser's WSS AOR keeps its REGISTER keepalive instead of UDP OPTIONS.

Do **not** paste `.env` into a ticket. Check only non-secret values locally:

```bash
grep -E '^(ASTERISK_EXTERNAL_ADDRESS|IMAGE_TAG|ASTERISK_RTP_START|ASTERISK_RTP_END)=' .env
```

The admin panel's **Service address** (`service_host`) is authoritative; it
must resolve to the VM's public numeric IP, not a Cloudflare-proxied hostname.
Keep `SECRET_KEY` unchanged across image upgrades. UDP 10000–10100 must reach
the Asterisk container, and the phone must re-register before its contact
expires. If `pjsip show contacts` shows the Zoiper contact as `Unreachable`,
fix registration/NAT/firewall before debugging browser audio.

## Premium in-call screen

The active call screen is now a full-height glass shell instead of one long
scrolling column. The caller, timer, codec badge and audio-unlock prompt live in
a fixed header; mute/hold/keypad and DTMF live in a scrollable body;
**End Call, Answer/Decline and the live status live in a fixed footer**. Opening
the DTMF keypad can no longer push End Call below the window. The screen has
distinct dialling, ringing, connected and ended phase styling, larger touch
targets, `touch-action: manipulation`, reduced-motion support, and short-window
rules. Versioned `/phone.css?v=13` / `/phone.js?v=13` and service-worker cache
`eip-phone-v13` ensure the new phone code replaces a cached old call screen.
