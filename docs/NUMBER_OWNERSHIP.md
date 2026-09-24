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
4. Both legs are bridged and recording policy applies.

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
