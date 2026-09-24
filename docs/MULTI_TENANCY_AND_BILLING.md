# Customer tenancy, provisioning, and billing

EIP Telephony Control separates platform administration from customer self-service.

## Signup and provisioning

Public signup at `/` creates an active customer login with only username, email, and a password of at least 14 characters. It creates no phone number, SIP provider, or extension automatically. This prevents a new account from consuming carrier resources before the platform administrator provisions it.

The platform administrator:

1. Configures upstream SIP providers. Provider credentials and trusted network settings are never returned to customers.
2. Creates or assigns an extension to a customer account.
3. Assigns one or more phone numbers to that customer and chooses the upstream provider and owned inbound extension.
4. Sets each number's monthly price, billing-cycle day, billing start, and optional discontinuation date.

A customer may create additional extensions, but every created resource is stamped with that authenticated customer's immutable owner ID. Phone numbers can only route to extensions owned by the same customer.

## Tenant boundary

Customers see only their own:

- phone numbers and subscription metadata;
- extensions and voicemail mailboxes;
- call history and recordings for those extensions;
- API keys and webhook endpoints;
- webhook delivery history;
- invoices.

The boundary is enforced server-side for panel sessions and scoped API keys. It is not a UI-only filter. API call lookup, hangup, recording playback, voicemail files, extension queries, number queries, webhook changes, and API-key deletion all verify ownership. Upstream SIP providers are platform-only.

Existing resources from an older installation remain platform-owned until an administrator assigns them to a customer. This makes it possible to assign the current platform number to a customer such as `engineeip` without recreating the carrier configuration.

## API keys

Each customer can create scoped API keys. The full secret appears once in a dedicated copy panel and only its prefix and hash are retained. Deleting a key permanently invalidates it. A customer key can query or control only resources owned by that customer.

## Number billing

The default price is **USD $5.00 per phone number per month**. Administrators may set a different price for an individual number. Billing is invoice-only in this release; payment occurs outside EIP Telephony Control.

The system creates one invoice record per active customer number and billing period. Invoices include the number, period, amount, status, and due date. Duplicate invoices for the same number and period are prevented. A customer can request discontinuation from **Billing & invoices**; the system schedules it for the next monthly renewal date rather than cutting off a paid period immediately. Administrators can also set a specific date. A scheduled discontinuation automatically makes a number inactive on or after that date, preventing inbound/outbound use after configuration synchronization.

Deleting a customer is blocked while the account still owns numbers or extensions. Reassign or discontinue numbers and remove/reassign extensions first, preserving explicit lifecycle control and auditability.

## Security notes

- Signup is rate-limited and does not expose telephony resources.
- Customer email and username are unique.
- Carrier credentials remain administrator-only.
- Ownership is based on database user IDs, not user-supplied names.
- Customer extension edits cannot claim an extension owned by another customer.
- A number cannot be assigned to another customer's extension.

## Provision the EngineerIP customer on a deployed VM

Run the interactive command inside the API container so it uses the deployed encrypted settings database and `SECRET_KEY`:

```bash
docker compose exec telephony-api python -m app.manage_customer --username engineerip
```

The command securely prompts for the customer email and password (including confirmation); the password is not placed in shell history, logs, `.env`, or Git. It creates the active `engineerip` customer, or safely updates that customer's email/password if it already exists. It refuses to overwrite a platform administrator. It deliberately assigns no provider or number; use the administrator panel afterward to transfer the existing number or provision another one.
