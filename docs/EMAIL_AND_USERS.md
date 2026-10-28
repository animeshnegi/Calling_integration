# Voicemail Email and Admin Users

## Overview

**Elastic Email** is the only sender for voicemail notifications; there is no provider switch. New messages are emailed to the receiving extension with the original WAV/GSM audio attached and, optionally, a locally generated transcript. Only administrators can configure the provider; the API key is encrypted and is never returned to the browser.

## Configure Elastic Email

1. Open **Email Delivery**. It always sends through **Elastic Email**.
2. If no key is saved, enter the Elastic Email API key with **Send HTTP** permission, without `Bearer`. Use an address or domain verified in that Elastic Email account as the sender.
3. Enter the sender name and a test recipient, then select **Save & send test email**. The test saves the current settings first and works while automatic notifications are disabled.
4. If the provider accepts the test, check the inbox/spam and Elastic Email activity. Acceptance means queued, not delivered.
5. Turn on **Enable voicemail email** and set each extension's notification address to receive future voicemails.

The integration uses the legacy v2 `https://api.elasticemail.com/v2/email/send` endpoint, matching the working Python example. It sends `apikey`, `subject`, `from`, `fromName`, `to`, `bodyText`, `bodyHtml` and `isTransactional` as POST form fields. Voicemail audio is attached as a multipart file, not sent to a transcription service. It checks both HTTP status and the v2 JSON `success` flag; HTTP 200 with `success: false` is an error. The API key is in the request body, not the URL, and redirect following is disabled.

An Elastic Email key saved in the old SendGrid-labelled field is reused automatically: there is no need to change providers or re-enter it. Existing sender settings and the email-enabled switch are preserved. As a safety measure, a saved key recognisably beginning with `SG.` is not forwarded to Elastic Email; replace it with an Elastic Email key. Local transcription requires no API key.

The API key is encrypted in the settings database using a key derived from `SECRET_KEY`. Admin state returns only `provider` and `has_api_key`; it never returns the key. Keep `SECRET_KEY` unchanged and backed up securely. Provider error bodies and credentials are not echoed; the interface displays safe diagnostic hints. The browser requests a structured test result so a reverse proxy cannot replace a provider rejection with a generic 502 page; older test clients retain the previous HTTP 502 failure contract.

## Set a recipient

Administrators can set **Voicemail notification email** while adding or editing an extension. An extension user can update their own address under **Security > My voicemail email**. Updating a user's own email also updates the assigned extension's voicemail recipient.

Automatic email requires an active, voicemail-enabled extension with a valid notification email, an enabled and configured provider with a verified sender, and a new or urgent voicemail. The application's attachment cap is 20 MiB; provider/account email-size limits can be lower. The audio is still available in the voicemail inbox if email delivery fails.

## Delivery processing

The ARI worker scans voicemail roughly every 30 seconds, fingerprints the message, claims it atomically, and submits the text/HTML and audio to Elastic Email. Failed attempts retry up to five times; changing the key or sender resets failed attempts, but accepted messages are not sent again. A timeout can have an unknown result; check provider activity before retrying to avoid duplicates. Provider acceptance does not guarantee inbox delivery.

For an image-only deployment, build/export the API image on Windows, load it on the VM, and recreate both `telephony-api` and `telephony-ari` with `--no-build`; do not rebuild the VM or change Asterisk, proxy, outbound calling, `.env`, or the existing volumes. SQLite/MySQL automatically migrate legacy provider settings to Elastic Email and reset previous SendGrid HTTP 4xx rejections once, without resetting accepted mail or ambiguous timeouts. Live account credentials, network reachability and actual inbox delivery still require a test in the deployed environment.

## User roles

### Administrator

Administrators have system-wide access to:

- extensions, phone numbers and SIP providers;
- all calls, recordings and voicemail;
- webhooks, call settings and email delivery;
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

- Use a restricted provider key with only email-send access, not an account-wide key.
- Rotate the key immediately if exposed.
- Use HTTPS for the admin panel.
- Do not share the bootstrap administrator account.
- Give normal staff the Extension user role, not Administrator.
- Disable accounts immediately when staff leave.
- Protect email attachments according to call-recording and privacy policy.
- Obtain required consent before recording or emailing voicemail.
- Monitor provider activity and rejected/bounced messages.
- Back up `telephony_data`; it contains encrypted settings and delivery state.

## Troubleshooting

### Test email fails

Check the Elastic Email API key and Send HTTP permission, the verified sender, account restrictions and the VM's outbound HTTPS/DNS. A test does not require automatic delivery to be enabled. All messages use `api.elasticemail.com`; the inline error does not include the key or raw provider response. If a new test still says `SendGrid HTTP 401` after the image update, the browser or API is running the older release: refresh the page and verify that the newly built image was loaded and both API/ARI services were recreated.

### Test succeeds but voicemail email is not sent

Confirm the extension has a notification email, the message is still New/Urgent, the ARI worker is running, and the attachment is below 20 MiB. Inspect:

```bash
docker compose logs --tail=200 telephony-ari
```

### User sees another extension

This should be blocked by backend scope enforcement. Disable the account, preserve logs, and report the exact endpoint/request. Verify the account role and assigned extension in **Users**.
