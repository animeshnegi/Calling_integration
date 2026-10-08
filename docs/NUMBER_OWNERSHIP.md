# Numbers, Extension Sets and How a Call Reaches a Phone

## The model

```text
EIP telephony
├── account "Meridian Health"                     (a customer)
│   ├── user  a person who signs in
│   ├── +13025550001   its own set: 101 102 104 …              (each set starts at 101)
│   ├── +13025550002   its own set: 101 102 104 …
│   └── +13025550098   its own set: 101 102
├── account "Northwind Trading"
│   └── +13025550011   its own set: 101 102 …                  (its 101 is not Meridian's 101)
└── the platform's own line (the operator's devices, no number)
```

An account has any number of users, a user has any number of numbers, and **every
number carries its own extension set, counting from 101 up**. 101 on
+13025550001 and 101 on +13025550002 are two different phones, and so are the
101s of two different accounts. The same digits may appear on every number of an
account - that is the point, not a clash.

**A three-digit extension is resolved only within the current phone number.**
That one rule shapes everything below: dialling, inbound calls, IVR menus, call
flows, transfers, ring groups, voicemail, recordings, the API and the rendered
Asterisk configuration.

## Identity

An extension is identified in the database by **the number it belongs to and its
digits**:

| Where | Value | Example |
| --- | --- | --- |
| `extensions.id` | stable primary key (never shown to a customer) | `41` |
| `extensions.extension` | the three digits a person dials | `101` |
| `extensions.phone_number_id` | the line it answers on (NULL: no line yet) | `7` |
| constraint | `UNIQUE(phone_number_id, extension)` | - |
| the key the app speaks | digits `@` number | `101@+13025550001` |
| PJSIP endpoint / dial plan name | digits `-` number | `101-13025550001` |
| voicemail mailbox / folder | digits `-` number | `101-13025550001` |
| SIP username (technical, never dialled) | tag `_` digits `_` number | `MERIDIAN_101_13025550001` |

A PJSIP section name cannot contain `@` (Asterisk reads it as a key/value pair),
which is why the endpoint uses a hyphen. **Digits alone are never an endpoint
name** while two lines can hold them: `PJSIP/101` does not exist, so it can never
be ambiguous. Only the platform's own rows - the operator's devices, which belong
to no customer number - keep their bare digits, because they answer in the
operator's single context.

The SIP username is a **technical identifier**: a device signs in with it, and no
person ever dials it. The customer dials the digits; the platform resolves them
against the number the call is on.

## What happens when somebody dials

| Caller dials | On | Reaches | Why |
| --- | --- | --- | --- |
| `104` | a phone on +13025550001 (which has 104) | `104@+13025550001` | three digits resolve inside this number |
| `104` | a phone on +13025550098 (which has none) | NOT IN SERVICE | the current number has no 104; nothing is borrowed from another line |
| `104` | a caller in +13025550002's menu (which has 104) | `104@+13025550002` | the menu is that line's |
| `13025550002` | a phone on +13025550001, same customer | `101@+13025550002`, that number's own inbound destination | the number is looked up internally, and never handed to the carrier |
| `+13025550002` | the same phone | the same device | the `+` form works the same way |
| `13025550011` | a Meridian phone | an ordinary external call over the carrier | another organisation's number is not the platform's to route |
| `+919812345678` | any phone | the carrier trunk, presenting that line's own number | anything the platform does not own is outbound |

A line that lacks the digits plays **NOT IN SERVICE** - never another number's
104, the account's lowest line, the first match, or another customer's desk. The
same lookup is used everywhere: normal dialling, inbound routing, IVR menus, call
flows, internal calls triggered through the API, transfers (blind and attended),
ring groups and voicemail.

**Another customer's extensions are not reachable by their digits.** A customer's
context contains its own numbers' sets and nothing else, so a misdial is
`ss-noservice` and never a crossed line; customer-to-customer internal extensions
are not exposed through three-digit dialling at all.

## Inbound calls

A call arriving on a DID rings the extension that **that number's own link**
names (`phone_numbers.inbound_extension`, stored as the key), and the flows
written for that number - which may only ring that line's own extensions. A
number that cannot ring one of its own extensions is not in service rather than
somebody else's.

## Dialling by number and by extension

There is exactly one numbering plan per line and no hidden second one:

* **Three digits** - an extension of the current number, and of no other.
* **A full phone number** - the platform's own numbers are routed internally,
  straight to the destination number's inbound destination; a number the platform
  does not own leaves over the carrier. Both the plain digits and the `+` form are
  rendered as literal routes, because the phone decides which one it sends, and a
  literal always beats the outbound patterns.
* **The SIP username** - never customer-dialable, and never rendered as an
  extension.

## Outbound calls and caller ID

The caller ID comes from **the current phone number**, not from the digits:

```text
101 on +13025550001  dials out presenting +13025550001
101 on +13025550002  dials out presenting +13025550002
```

Each rendered endpoint carries `set_var=OUTBOUND_CID=<its own number>` and
`set_var=OUTBOUND_TRUNK=<carrier>`, so a device that dials a pattern in its own
context presents its own line even when two 101s exist. A device with no number
of its own is refused outbound dialling (`ss-noservice`) instead of borrowing
somebody else's identity.

`POST /api/v1/calls` accepts `extension` as the key (`101@+13025550001`) or as
the digits while they are unambiguous, and the presented number must be an active
number of the calling account.

## Voicemail

Voicemail is **number-scoped**: the mailbox is `101-13025550001`, so two 101s on
two numbers have two separate boxes with their own PINs, greetings and messages.
`*97` reaches the caller's own box, and the rendered `voicemail.conf` and
`VoiceMail(...)` steps all use the same mailbox name.

## WebRTC

* **WebRTC off** - the extension registers and is dialled on its plain PJSIP
  endpoint (UDP/TCP, plain RTP). The browser has no endpoint of its own.
* **WebRTC on** - the browser identity is added as a second endpoint over the WSS
  transport (`webrtc=yes`, DTLS-SRTP, ICE, RTCP-mux), and the plain endpoint is
  left exactly as it was: **checking WebRTC never disables normal SIP.** Hardware
  phones and softphones keep registering with the same credential.

An extension whose box is ticked is dialled on its WebRTC endpoint (dialling
extension, ring group, IVR selection, call flow and the console's click-to-call
all follow it); everything else stays on the plain endpoint.

## The administration panel and the API

Open **Phone numbers** for a customer and each row is that customer's line with
its own devices: the set it holds (`101, 102, 104`), how many extensions it
carries, which extension it answers on, and an **Add extension** action that adds
the next free extension *of that line* (`102` on a line that has no 102, even when
the customer's other line already has one).

* `POST /admin/api/numbers/<number>/extensions` - create the next (or a named)
  extension on that number: `{"extension": "104"}`.
* `GET /admin/api/numbers/<number>/extensions` - that number's own set.
* `GET /admin/api/extensions/next?number=<number>` - the next free digits on that
  number.

Creation validates the pair (number, digits), not the digits alone. Every
password, call-flow, group, voicemail, WebRTC, recording and SIP-credential
endpoint resolves the number-scoped extension: a key names one row exactly, and
bare digits are accepted only while they name one extension of that account - with
several, the request is refused with

```text
101 is on more than one of your numbers - name the phone number as well
```

so a change can never silently land on the wrong desk. The extensions page groups
rows by number and shows `101 · +13025550001`, so the same digits on two lines are
never confused.

## Legacy data and migration

An install upgrading from an older release is migrated once, on startup, without
deleting or recreating a device:

* rows that stored bare digits (`101`) or a key (`101@+13025550001`) are moved onto
  `(phone_number_id, extension)`, keeping their id, credentials, mailbox,
  recordings, owner and every flow that names them;
* a row is placed on the number its inbound link names, then on the account's
  primary line, and a row that has no line at all is **left unassigned** (nothing
  dials it; the console lists it under **No number yet** and it is published in
  `extensions_unassigned`) rather than being merged into another extension;
* a device account keeps the key that says which line's extension it signs in as;
  a bare legacy link becomes that key when exactly one row of that account answers
  those digits, and is left alone when two lines could mean it;
* two rows carrying the same digits **are never merged**: they are two devices on
  two lines.

## Limits and notes

* Extensions are three digits, `100`-`999`, so a number can hold up to 899
  devices. The customer asked for "101 to infinite": four-digit extensions would
  be a deliberate change to the validation, the dial patterns (`_XXX`) and the
  mailbox naming.
* The platform's own extensions (operator devices) have no customer and no line;
  they answer in `[from-internal]` only, and a customer's calls never reach them.
* **Moving an extension** to another account stays a deliberate administrator
  action (the console sends `reassign` when the *Customer account* field changes):
  it is written to the activity log, the account that lost it is notified, and its
  phones simply no longer have those digits in their context.
* The outbound patterns are `_+X.` and `_XXXX.` (a leading `+` or five digits), so
  three digits cannot leave the platform. The ARI engine enforces the same rule
  when the call arrives, so a stale link is a missed call and never a crossed line.

## Verifying it

* `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q` - the whole suite, including
  duplicate 101s working independently, `104` never crossing to another number,
  the per-number caller ID and mailbox, WebRTC on/off, and the migration.
* `PYTHONPATH=. .venv/bin/python tools/dialcheck.py` - renders the production
  scenario (+13025550001 and +13025550002 both holding 101/102/104, +13025550098
  holding 101/102, plus a second customer) through the production renderer and
  prints where each dialled number lands.
* `PYTHONPATH=. .venv/bin/python tools/livecheck.py` - runs the same promises over
  HTTP against a seeded preview, including adding a device to one line through
  `POST /admin/api/numbers/<number>/extensions`.
* `node tools/uicheck.js` (jsdom) - the console: two lines with their own 101s are
  shown apart, the add-extension action lands on the right line, and the dialog
  names the line that already holds the digits.

## Verification procedure on a live platform

For each extension:

1. Assign its number and note which line the device belongs to.
2. Register only that device (Zoiper, hardware phone, or the browser phone).
3. Place an outbound call from the panel and confirm the customer sees the
   number of that device's line.
4. Call the DID from an external phone and confirm the line's own device rings -
   and that the other line's device with the same digits does not.
5. Dial `104` from a phone on a line that has one: confirm that line's 104 rings.
   Dial `104` from a line that has none: confirm NOT IN SERVICE.
6. Dial another number of the same account in full: confirm its own device rings
   and `asterisk -rvvv` shows `Dial(PJSIP/101-<digits>)`, not a carrier leg.
7. Repeat for every number and device.

If the customer sees the trunk username instead of the DID, contact the carrier
and confirm caller-ID/PAI authorization. If a call reaches the wrong device,
check `phone_numbers.inbound_extension` - it stores the key, so `101@+1302…`
names one line's 101 exactly.
