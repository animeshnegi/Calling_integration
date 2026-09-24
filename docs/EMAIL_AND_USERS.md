# SendGrid Voicemail Email and Admin Users

## Overview

The main administrator can configure one SendGrid account and create additional admin-panel users. New voicemail messages can be emailed to the address assigned to the receiving extension, with the WAV/GSM message attached.

SendGrid configuration is intentionally available only in the authenticated main admin panel. It is not loaded from browser JavaScript or returned by any API response.

## Configure SendGrid

1. In SendGrid, create a restricted API key with only Mail Send permission.
2. Verify the sender address/domain in SendGrid.
3. Sign in as an administrator and open **Email delivery**.
4. Enter the SendGrid API key, verified sender email, and sender name.
5. Enable voicemail email and save.
6. Enter a test recipient and use **Send test email**.

The API key is encrypted in `settings.db` using a key derived from `SECRET_KEY`. The admin state returns only `has_api_key`; it never returns the key. Keep `SECRET_KEY` unchanged and backed up securely.

## Set a recipient

Administrators can set **Voicemail notification email** while adding or editing an extension. An extension user can update their own address under **Security > My voicemail email**. Updating a user's own email also updates the assigned extension's voicemail recipient.

Email delivery requires all of the following:

- extension is active and voicemail is enabled;
- extension has a valid notification email;
- SendGrid delivery is enabled;
- API key and verified from-address are configured;
- message is in New (`INBOX`) or Urgent;
- attachment is no larger than 20 MiB.

## Delivery processing

The dedicated ARI worker scans voicemail every 30 seconds. For each new message it:

1. hashes mailbox plus audio to form a stable delivery fingerprint;
2. atomically records a delivery attempt in the configured database;
3. sends text and HTML bodies with the audio attachment through `https://api.sendgrid.com/v3/mail/send`;
4. marks HTTP 202 responses delivered;
5. retries failures up to five times;
6. never resends a delivered fingerprint, including after Asterisk message renumbering.

SendGrid errors are not exposed to extension users. Check ARI-worker logs and the `voicemail_deliveries` table when troubleshooting.

## User roles

### Administrator

Administrators have system-wide access to:

- extensions, phone numbers and SIP providers;
- all calls, recordings and voicemail;
- webhooks, call settings and SendGrid;
- user creation, update, disabling and deletion;
- their own profile email and password.

The system prevents an administrator from deleting their own account, disabling/demoting themselves, or removing the last active administrator.

### Extension user

An extension user is assigned exactly one active extension and can access only:

- dashboard totals for the assigned extension;
- call history for that extension;
- recordings belonging to that extension;
- voicemail belonging to that mailbox;
- their own email address and password.

Backend authorization enforces this scope. Hiding sidebar items is only a UI convenience and is not the security boundary. Requests that try to select another extension are overridden or rejected.

## Create a user

1. Open **Users** as an administrator.
2. Select **Add user**.
3. Set username, optional email, role, assigned extension, and an initial password of at least 14 characters.
4. Keep the account active and save.
5. Give credentials to the user through a secure channel and ask them to change the password.

For extension users, an assigned active extension is mandatory. Administrators may be unassigned and can access all extensions.

## Security recommendations

- Use a restricted SendGrid key, not an account-wide key.
- Rotate the key immediately if exposed.
- Use HTTPS for the admin panel.
- Do not share the bootstrap administrator account.
- Give normal staff the Extension user role, not Administrator.
- Disable accounts immediately when staff leave.
- Protect email attachments according to call-recording and privacy policy.
- Obtain required consent before recording or emailing voicemail.
- Monitor SendGrid activity and rejected/bounced messages.
- Back up `telephony_data`; it contains encrypted settings and delivery state.

## Troubleshooting

### Test email fails

Check that delivery is enabled, the API key is valid, the sender is verified, the VM can reach `api.sendgrid.com` over HTTPS, and SendGrid has not suspended or limited the account.

### Test succeeds but voicemail email is not sent

Confirm the extension has a notification email, the message is still New/Urgent, the ARI worker is running, and the attachment is below 20 MiB. Inspect:

```bash
docker compose logs --tail=200 telephony-ari
```

### User sees another extension

This should be blocked by backend scope enforcement. Disable the account, preserve logs, and report the exact endpoint/request. Verify the account role and assigned extension in **Users**.
