# Numbers, Extension Sets and How a Call Reaches a Phone

## The model

```text
EIP telephony
├── account "Meridian Health"                     (a customer)
│   ├── user  a person who signs in
│   ├── +13025550098   its own set: 101 102 103 104 …         (each set starts at 101)
│   ├── +13025550067   its own set: 101 102 103 104 …
│   └── +13025559999   its own set: 101 …
├── account "Northwind Trading"
│   └── +13025550011   its own set: 101 102 …                 (its 101 is not Meridian's 101)
└── the platform's own line (the operator's devices)
```

An account has any number of users, a user has any number of numbers, and **every
number carries its own extension set, counting from 101 up**. 101 on
+13025550098 and 101 on +13025550067 are two different phones, and so are the
101s of two different accounts.

Stored as a **key**, the extension's identity everywhere in the platform:

```text
101@+13025550001        the first device of line +13025550001
101                     an account-wide extension (kept from before this change)
```

Everything else is derived from the key, so nothing has to guess:

| Where | Value | Example |
| --- | --- | --- |
| `extensions.extension` | the key | `101@+13025550001` |
| what a caller dials | the digits | `101` |
| voicemail mailbox / folder | digits + number | `101-13025550001` |
| PJSIP endpoint name | the digits, or digits + number | `101` or `101-13025550001` |

A PJSIP section name cannot contain `@` (Asterisk reads it as a key/value pair),
so the endpoint is named with a hyphen. The one extension that owns the plain
three-digit name keeps it - that is the name a hand-written dial plan and a
device account already use - and every other row answers on `digits-number`.

## What happens when somebody dials

| Caller dials | On | Reaches | Why |
| --- | --- | --- | --- |
| `101` | any phone of the account | the account's device with those digits | the digits name one device inside one organisation |
| `104` | a caller in +13025550098's menu | `104@+13025550098` | the menu is that line's, and a line only rings its own devices |
| `104` | a phone that is on +13025550098 | one `104` of the account (first line that has one) | dial-by-extension is per account, not per line |
| `13025550067` | a phone of the same account | `101@+13025550067`, that number's own 101 | the number is looked up internally, and never handed to the carrier |
| `13025550011` | a Meridian phone | "not in service" | another organisation's number is an ordinary external call, and its extensions are not in this context |

**Inbound.** A call arriving on a DID rings the top of *that number's* set: the
extension the number is linked to (`phone_numbers.inbound_extension`, the stored
key), and the flows written for that number - which may only ring that line's own
extensions plus the account-wide ones. So the same digits on two lines never
collide, which is the whole point of per-number sets.

**Dial-by-extension inside an account.** The digits name exactly one device in a
customer's context, which is what makes day-to-day PBX life work: an employee
types 104 and reaches the 104 they mean, whichever line that desk sits on. Where
two of the account's lines hold the same digits, the lowest line number wins, and
the flow builder is where a caller is steered to a *specific* line's device.

### Reaching another number's extensions

Extensions other than the account's own are reached by dialling the other
**number** - the way the outside world does:

```text
a desk on +13025550098 dials 13025550067
        ↓
the lookup happens inside the account that owns the calling phone
        ↓
13025550067 belongs to this account, and its inbound extension is 101@+13025550067
        ↓
Dial(PJSIP/101-13025550067)      ← an internal call; it never leaves the platform
```

Both the plain digits and the `+` form are rendered as literal routes, because
the phone decides which one it sends, and a literal always beats the outbound
patterns. A number is only rendered as a local shortcut when it can really ring a
device of its own account; otherwise the call is the ordinary external call it is
and leaves over the carrier.

There is no hidden second numbering plan: one set of digits per number, the
account's digits for internal dialling, and numbers-for-numbers when a specific
line's device is what the caller wants.

## The administration panel

Open **Phone numbers** for a customer and each row is that customer's line with
its own devices:

* the set it holds (`101, 102, 103`), how many extensions it carries, and which
  extension it answers on;
* **Add extension** - the next free extension *of that line* (`102` even when the
  customer's other line already has a 102);
* **Call flow** - the flow of the extension that answers the number.

The extension dialog asks which number the extension belongs to (the customer's
active numbers, plus an *account-wide* choice for older deployments), and says
which line already holds the digits being typed. Each row on the extensions page
reads `101 · +13025550001`, so the same digits on two lines are never confused. A
customer's extensions page is grouped by number for the same reason.

Credentials are per extension and unchanged in shape: the generated SIP username,
a password the customer may reset, the registration server and transport. An
extension with no line of its own calls out as the account's main line, which is
what keeps a row written before this change working.

### Call defaults and caller ID

A customer's outbound and fallback extension are stored as keys
(`{"outbound": "102@+13025550001", "fallback": "101@+13025550001"}`). Digits are
still accepted when they are unambiguous; when two of the customer's numbers both
hold them the request is refused with

```text
104 is on more than one of your numbers - choose the extension on the number it belongs to
```

so a default can never silently land on the wrong desk. The same message guards
call-flow destinations and ring groups.

A number carries one default outbound extension, and an extension may be the
default of one number: choosing a new default clears the previous one - both the
number it pointed at and the link itself.

## Legacy and limits

* **Account-wide rows.** A bare `101` (`extension` with no `@`) answers on every
  number of its account. That is what every row written before numbers had their
  own sets is, so nothing was migrated and no device was lost. New rows are
  always created on a number; the account-wide choice is offered for the
  platform's own line and for compatibility.
* **Digits.** Extensions are three digits, `100`-`999`, so a number can hold up
  to 899 devices. The customer asked for "101 to infinite": four-digit extensions
  would be a deliberate change to the validation, the dial patterns (`_XXX`) and
  the mailbox naming, and the platform currently reads anything that is not three
  digits as an external number.
* **Digits are never SIP identities.** The user dials the digits and the platform
  resolves them; the generated username (`KUDGTE_101`) is what the phone
  authenticates with, and it is unique platform-wide (`idx_extensions_sip_username`).
  A device account may not take a username that is an extension's digits or key.
* **Moving an extension** to another account stays a deliberate administrator
  action (the console sends `reassign` when the *Customer account* field
  changes): it is written to the activity log, the account that lost it is
  notified, and its phones simply no longer have those digits in their context.
* **Another account's digits are absent from the context**, so a misdial is
  "not in service" (`ss-noservice`) and never a carrier call: the outbound
  patterns are `_+X.` and `_XXXX.` (a leading `+` or five digits), so three
  digits cannot leave the platform. The ARI engine enforces the same rule when
  the call arrives, so a stale link is a missed call and never a crossed line.

## Outbound calls and callback routing

```text
Extension 101 calls a customer using +13025550101
Customer calls +13025550101 back
Carrier sends the DID to Asterisk
Asterisk rings the extension that number's own link names
```

The dial plan writes an explicit route per DID: an exact `Dial(PJSIP/<name>)`
line for both the plain digits and the `+digits` form, because carriers differ.
There is no ring-all group. The employee-first flow is unchanged: the extension's
phone rings, the customer leg is created only after it is answered, the presented
number must be an active number of that account, and both legs are bridged with
recording decided by the extension's own switch.

`POST /api/v1/calls` accepts the same inputs as before; `extension` may be the
digits or the key, and the caller-ID number must belong to the calling account.
An extension that belongs to a number presents that number; an account-wide or
platform extension presents the account's main line.

## Carrier requirements

Asterisk can request the number, but the carrier makes the final decision about
what the called party sees, so the platform's own checks are only half of it:

* every presented number must be bought or verified for the account at the
  carrier;
* inbound delivery for every DID must point at this platform's SIP trunk - a
  number that is not delivered here simply never rings;
* P-Asserted-Identity / Remote-Party-ID must be accepted if the authenticated
  `From` user stays the trunk username;
* the provider's source addresses must be in the configured IP/CIDR allowlist.

Never permit arbitrary caller ID input. This implementation only accepts numbers
already stored, active, and owned by the account placing the call.

## Verifying it

* `PYTHONPATH=. .venv/bin/python tools/dialcheck.py` renders this exact scenario -
  one customer with two numbers whose sets both start at 101, plus a second
  customer - through the production renderer, and prints where each dialled
  number lands. Every check is a promise from this document.
* `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q` covers provisioning,
  per-number resolution, the dial plan, the endpoint names and the refusals.
* `.venv/bin/python tools/livecheck.py` runs the same promises over HTTP against
  a seeded preview, including adding a device to one line through
  `POST /admin/api/numbers/<number>/extensions`.
* `node tools/uicheck.js` (jsdom) checks the console: two lines with their own
  101s are shown apart, the add-extension action lands on the right line, and the
  dialog names the line that already holds the digits.

## Verification procedure on a live platform

For each extension:

1. Assign its number and note which line the device belongs to.
2. Register only that device (Zoiper, hardware phone, or the browser phone).
3. Place an outbound call from the panel and confirm the customer sees the
   number of that device's line.
4. Call the DID from an external phone and confirm the line's own device rings -
   and that the other line's device with the same digits does not.
5. Dial the digits from a phone of the account and confirm the expected desk
   rings; dial the other number of the account and confirm its own device rings
   without touching the carrier (`asterisk -rvvv` shows the `Dial(PJSIP/…)`).
6. Repeat for every number and device.

If the customer sees the trunk username instead of the DID, contact the carrier
and confirm caller-ID/PAI authorization. If callbacks reach the wrong device,
check `phone_numbers.inbound_extension` - it stores the key, so `101@+1302…`
names one line's 101 exactly.
