# Extension Number Ownership and Callback Routing

## Required behavior

A phone number/DID is assigned to exactly one employee extension for callback routing:

```text
Extension 101 calls a customer using +13025550101
Customer calls +13025550101 back
Carrier sends the DID to Asterisk
Asterisk matches +13025550101 to extension 101
Only extension 101 rings
```

The dialplan does not create a ring-all group. Every configured DID has an explicit `Dial(PJSIP/<assigned-extension>)` route. Both digits-only and leading-plus DID forms are rendered because carriers may use either Request-URI format.

## Administration panel

Open **Phone numbers** and add/edit a number with:

- E.164 number;
- SIP provider;
- employee/inbound extension;
- description;
- active state;
- **Default outbound caller ID for this extension**.

An extension may own multiple numbers, but only one is its default outbound number. Selecting a new default atomically clears the previous default for that extension.

Administrators can assign and reassign numbers. Extension users see only numbers assigned to their extension and may choose which assigned active number is their default. They cannot assign themselves another employee's number or edit provider/routing configuration.

## User calling panel

The **Call history** page includes **New outbound call**. The user enters an E.164 customer number and selects one of their assigned callback numbers. The flow remains employee-first:

1. Asterisk rings the employee's assigned extension.
2. Only after the employee answers, Asterisk originates the customer leg.
3. The selected assigned DID is sent as caller ID using ARI `callerId`, P-Asserted-Identity and Remote-Party-ID support.
4. Both legs are bridged and the recording setting of the extension that records applies.

A non-admin user's requested extension is ignored and replaced by the extension stored on their account. A requested caller ID must be active and assigned to that same extension. Calls are rejected if no callback number is assigned.

Administrators can select any active extension, but the selected caller-ID number must still belong to that extension.

## CRM API

`POST /api/v1/calls` accepts an optional assigned caller ID:

```json
{
  "phone": "+919876543210",
  "extension": "101",
  "caller_id_number": "+13025550101",
  "contact_id": "582",
  "member_id": "37"
}
```

If `caller_id_number` is omitted, the extension's default active number is selected, falling back to its first active assigned number. If supplied, it must belong to the requested extension. The number's provider determines the outbound SIP endpoint, preventing mismatched provider/caller-ID combinations.

The call response and all lifecycle webhook payloads include:

```json
{
  "extension": "101",
  "phone": "+919876543210",
  "caller_id_number": "+13025550101",
  "provider": "IPComms"
}
```

## Carrier requirements

Asterisk can request the assigned caller ID, but the carrier makes the final decision about the displayed number. In IPComms/carrier configuration:

- every presented number must be purchased or verified for the account;
- inbound delivery for every DID must target the VM's SIP trunk;
- P-Asserted-Identity/Remote-Party-ID must be accepted if the authenticated From user remains the trunk username;
- provider source addresses must be in the configured IP/CIDR allowlist.

Never permit arbitrary caller ID input. This implementation only accepts numbers already stored, active, and owned by the originating extension.

## Dialling another extension

This is the part that decides whether one employee can reach another, so it is
worth being explicit about what owns what.

**A number belongs to an extension; an extension belongs to an account.** The
extension number is the dialling key: three digits, unique platform-wide
(`next_extension_number`), and never reused by another account. A customer's
numbers all point into the same extension set, because the extensions belong to
the customer, not to the DID.

```text
Account "Meridian Health"
├── +13025550098  -> 20 extensions   101 … 120
├── +13025550067  -> 15 extensions   121 … 135
└── dial plan context from-internal-<meridian's id>
    ├── 101 … 135   every extension of the account, from either number
    └── the platform's own lines (extensions with no owner)

Account "Northwind Trading"
└── dial plan context from-internal-<northwind's id>
    ├── 301, 302
    └── the platform's own lines
```

* A device registers as its extension's generated SIP identity and answers in
  **its account's context** (`from-internal-<account id>`), written into the
  endpoint by the renderer.
* That context contains **every active extension of the account** - whichever
  number it belongs to - so 105 under `+13025550098` dials 117 by typing `117`,
  and 108 under `+13025550067` dials 102 under the other number exactly the same
  way. Two numbers on one account are two doors into one extension set; there is
  nothing to configure per number.
* The context also holds the platform's own lines (extensions with no owning
  account) - the operator's service line - and every customer can dial those.
* The platform's own devices answer in `[from-internal]`, which reaches every
  extension on the platform. That is the operator's support line, and the shape
  rows written before extensions had an owner keep.

### What happens when the extension belongs to somebody else

It is not in the context, so the call is answered with "the number you dialled
is not in service" (`ss-noservice`) and hung up. It cannot ring another
organisation's phone, and it does not fall through to a carrier trunk either:
the outbound patterns are `_+X.` and `_XXXX.` (five digits or a leading `+`),
so a three-digit misdial never leaves the platform. A pattern more specific than
`_XXX` - an exact extension, or a future short code - always wins over the
guard, so adding one later needs no thought about this rule.

The same rule protects inbound calls. A DID may only ring an extension of its
own account: if a stale row points a number at another organisation's
extension, the renderer falls back to the number's own account fallback, and
where there is none the DID plays "not in service" instead. The ARI engine
checks the same thing when the call arrives (`start_inbound`), so a misrouted
DID is a missed call, never a crossed line. The phone menu (IVR) asks for an
extension from that account's own list as well.

### Why a number cannot be taken from under a phone

Extension numbers are how people inside an organisation reach each other, so the
platform treats one as a customer's line:

* Creating a line on a number another account already holds is refused
  (`Extension 101 already belongs to …`), and the console's extension dialog
  says who holds the number as it is typed.
* A request that does not name an owner keeps the owner the extension already
  has, so an edit - or an older caller that never sent the field - cannot
  orphan a line into the platform's number space.
* Moving a live extension to another account is possible only as a deliberate
  administrator action (the console sends `reassign` when that extension's
  **Customer account** field is changed on the edit dialog). It is written to the
  activity log, the account that lost it is notified, and its phones simply no
  longer have that number in their dial plan - they hear "not in service" rather
  than reaching whoever holds it now.

So the answer to "what are the chances he will not reach the other's number" is:
within one account, zero by construction - every active extension of the account
is in the same context, and the numbers are unique platform-wide; across
accounts, the call cannot be completed at all, which is the intended outcome.

### Verifying it

* `python tools/dialcheck.py` renders this exact scenario (two numbers, 20 + 15
  extensions) and prints what each organisation can dial.
* `PYTHONPATH=. .venv/bin/python -m pytest tests/test_telephony_config.py -q`
  covers the per-account contexts, the endpoint contexts, the DID ownership rule
  and the takeover refusal.
* `.venv/bin/python tools/livecheck.py` reads the dial plan the running preview
  just generated and checks the same promises over HTTP.

## Verification procedure

For each extension:

1. Assign a unique active DID and mark it default.
2. Register only that employee's Zoiper/phone.
3. Place an outbound call from the panel and confirm the customer sees the assigned DID.
4. Call that DID from an external phone.
5. Run `asterisk -rvvv` and confirm the DID route selects the expected extension.
6. Confirm only that endpoint rings.
7. Repeat for every extension and DID.

If the customer sees the trunk username instead of the DID, contact the carrier and confirm caller-ID/PAI authorization. If callbacks reach the fallback extension, inspect the exact inbound Request-URI format and compare it with the generated digits and `+digits` routes.
