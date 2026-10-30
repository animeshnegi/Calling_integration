# Voicemail Email and Admin Users

## Overview

**Elastic Email** is the only sender for voicemail notifications; there is no provider switch. New messages are emailed to the receiving extension with the original WAV/GSM audio attached and, optionally, a locally generated transcript. Only administrators can configure the provider; the API key is encrypted and is never returned to the browser.

## Configure Elastic Email

1. Open **Email Delivery**. It always sends through **Elastic Email**.
2. If no key is saved, enter the Elastic Email API key with **Send HTTP** permission, without `Bearer`. Use an address or domain verified in that Elastic Email account as the sender.
3. Enter the sender name and a test recipient, then select **Save & send test email**. The test saves the current settings first and works while automatic notifications are disabled.
4. If the provider accepts the test, check the inbox/spam and Elastic Email activity. Acceptance means queued, not delivered.
5. Turn on **Enable voicemail email**. A new voicemail uses the associated customer's account email by default. In **Extensions**, choose an optional override address or **Do not send voicemail email** for each extension as needed.

The integration uses the legacy v2 `https://api.elasticemail.com/v2/email/send` endpoint, matching the working Python example. It sends `apikey`, `subject`, `from`, `fromName`, `to`, `bodyText`, `bodyHtml` and `isTransactional` as POST form fields. Voicemail audio is attached as a multipart file, not sent to a transcription service. It checks both HTTP status and the v2 JSON `success` flag; HTTP 200 with `success: false` is an error. The API key is in the request body, not the URL, and redirect following is disabled.

An Elastic Email key saved in the old SendGrid-labelled field is reused automatically: there is no need to change providers or re-enter it. Existing sender settings and the email-enabled switch are preserved. As a safety measure, a saved key recognisably beginning with `SG.` is not forwarded to Elastic Email; replace it with an Elastic Email key. Local transcription requires no API key.

The API key is encrypted in the settings database using a key derived from `SECRET_KEY`. Admin state returns only `provider` and `has_api_key`; it never returns the key. Keep `SECRET_KEY` unchanged and backed up securely. Provider error bodies and credentials are not echoed; the interface displays safe diagnostic hints. The browser requests a structured test result so a reverse proxy cannot replace a provider rejection with a generic 502 page; older test clients retain the previous HTTP 502 failure contract.

## Set a recipient

Each active extension has **Voicemail email delivery**:

- **Send to the associated account (or address below)** with a blank override uses the active customer's account email. Updating **Security > My account email** changes this destination automatically, without rewriting an extension's custom address.
- A nonempty **Override notification email** takes priority over the account email. Existing custom recipients are preserved; the previous delivery/retry behavior for them is unchanged.
- **Do not send voicemail email** disables both automatic and custom delivery for that extension; it does not delete voicemail. An older client that explicitly clears its address still turns email off, preserving its previous meaning.

On upgrade, previously blank recipients automatically use the associated account for **new recordings only**. Older recordings remain in the console and are not emailed as a surprise backlog. Switching from a custom address to the account or newly attaching a profile to an ownerless mailbox also starts a new-only cutoff. After an opt-out is lifted or a line/extension changes ownership, only new recordings are emailed to **any** recipient, including an override. Cutoffs persist across restarts. When the line and extension have different owners, email is paused—even for a saved override—until ownership is consistent. No address is guessed for ambiguous legacy profiles or inactive/missing account owners.

Email requires a configured, globally enabled Elastic Email sender, an active extension with voicemail enabled either by its switch **or an enabled voicemail step in a flow**, and a new or urgent recording. For number-scoped customer extensions, the customer and number owners must match; an ownerless legacy mailbox needs exactly one unambiguous active user profile explicitly attached to it or a custom address. The application's attachment cap is 20 MiB; provider/account email-size limits can be lower. The audio remains in the voicemail inbox if email or transcription fails.

Both the voicemail email and test email include: **“This is an automated message. The sending email address is not associated with a user. Please do not reply.”**

## Delivery processing

The ARI worker scans voicemail roughly every 30 seconds, fingerprints the message, claims it atomically, and submits the text/HTML and audio to Elastic Email. Failed attempts retry up to five times; changing the key or sender resets failed attempts, but accepted messages are not sent again. A timeout can have an unknown result; check provider activity before retrying to avoid duplicates. Provider acceptance does not guarantee inbox delivery.

For this combined calling/voicemail release, build and export **both the API and Asterisk images on Windows**, load them on the VM, and recreate `asterisk`, `telephony-ari`, and `telephony-api` with `--no-build` during a maintenance window. The Asterisk image fixes existing shared-volume permissions and sets the future mailbox umask, as well as supplying hold music. See [the image-only release guide](WEBRTC-RELEASE.md); no server build, proxy change, or outbound-routing change is required. Preserve `.env`, `SECRET_KEY`, the database and all voicemail volumes. The API and ARI worker must both use `/app/voicemail` and context `engineerip`; the current Compose file pins these, but `docker load` does not update an older host Compose file. Verify the ARI configuration after deployment as described in the guide.

SQLite/MySQL automatically migrate legacy provider settings to Elastic Email and reset previous SendGrid HTTP 4xx rejections once, without resetting accepted mail or ambiguous timeouts. Live provider acceptance and actual inbox delivery still require a test in the deployed environment.

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

A customer user can access their own assigned numbers and extensions, associated call records and mailboxes, and their own email address and password. A profile's optional extension reference is not required for voicemail on customer-owned numbers; the number and extension ownership establish the associated account.

Backend authorization enforces customer scope. Hiding sidebar items is only a UI convenience and is not the security boundary.

## Create a user

1. Open **Users** as an administrator and select **Add user**.
2. Enter a username, account email and an initial password of at least 14 characters. Keep the customer account active and save.
3. Assign the customer's number and extensions. The account email is the automatic voicemail destination unless an extension has an override or email is off.
4. Give credentials to the user through a secure channel and ask them to change the password.

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

Confirm **Enable voicemail email** is on, the sender is verified, the extension's delivery choice is not off, and its associated active account has an email (or a valid override is set). Check that the line and extension belong to the same account, the recording is new/unread and was made after the automatic-recipient cutover, and the ARI worker is running. Allow at least 10 seconds for the recording to settle and 30–60 seconds for a scan. Oversize or unsupported transcription never deletes the voicemail. Inspect **Recent email deliveries**, Elastic Email activity, inbox/spam and:

```bash
docker compose logs --tail=200 telephony-ari
```

The email test button does not test voicemail, attachments or transcription. To check speech recognition, enable **Include a voicemail transcript** and leave a fresh spoken English voicemail, or run the offline image test in [the release guide](WEBRTC-RELEASE.md). Transcription is best-effort; if it fails, the email still includes the audio.

### User sees another extension

This should be blocked by backend scope enforcement. Disable the account, preserve logs, and report the exact endpoint/request. Verify the account role and assigned extension in **Users**.
