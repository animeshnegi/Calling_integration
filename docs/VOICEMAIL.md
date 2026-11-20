# Voicemail Management

EngineerIP uses Asterisk `app_voicemail` with one mailbox per enabled extension. Mailbox definitions, PINs, message-waiting indicators, unanswered-call routing, browser playback, and CRM-facing APIs are integrated with the administration system.

## Mailbox names

An extension belongs to one phone number, and every number carries its own extension
set from 101 up - so the mailbox is named after both, `<digits>-<number>`:

```text
101@+13025550001   (the key in `extensions`)
        ↓
101-13025550001    (the mailbox folder, the VoiceMail() context argument and the
                    name the console shows)
```

**A three-digit extension is resolved only within the current phone number**, so two
101s on two numbers are two mailboxes with their own PIN, greeting and messages, and
never one shared box:

```text
+13025550001/101  →  101-13025550001
+13025550002/101  →  101-13025550002
```

`mailbox_name()` does that translation in one place, so the key, the mailbox name and
the folder can never drift apart. The APIs and the console accept the key or the
mailbox name anywhere, and the bare digits only while they name one extension of the
account - two matches are refused with an explicit ambiguity error rather than
guessed. A row with no number of its own keeps the bare `101` it was filed under, and
the authenticated endpoint selects its mailbox automatically when it dials `*97`.
The extension's PIN is stored encrypted on that exact extension row. Legacy
mailbox aliases point to the full scoped extension key, not ambiguous digits.

## Upgrading a mailbox that gained a number

An install that predates number-scoped extensions filed `101` under the folder
`101`. When the migration moves that row onto the phone number it answers on, its
messages move with it: at startup `101` is adopted into `101-13025550001`, once,
renumbered so nothing is overwritten, and the empty folder is removed
(`SettingsStore.adopt_legacy_mailboxes()`). The move only happens while those
digits name exactly one extension and no row keeps the bare digits as a mailbox of
its own - the operator's `900` stays `900`. When two lines could both mean the
same folder nothing is moved at all, because a customer's messages are never
merged into the wrong box; the folder is left where it is, untouched.

## Enable a mailbox

1. Open `/admin` and select **Extensions**.
2. Add or edit an extension.
3. Enable **Voicemail** and enter a 4–10 digit numeric PIN. Leaving the PIN blank on a later edit keeps the existing encrypted PIN.
4. Save. The service atomically renders `voicemail.dynamic.conf`, updates the dialplan, and reloads PJSIP, voicemail, and dialplan modules through private AMI.

PINs are encrypted in `settings.db` and are never returned by the API or admin state. New messages can also be sent as email attachments through Elastic Email; see [`EMAIL_AND_USERS.md`](EMAIL_AND_USERS.md).

## Call behavior

For an extension with voicemail enabled:

1. Asterisk rings the SIP endpoint for 30 seconds.
2. If it is not answered, the caller enters `VoiceMail(101-13025550001@engineerip,u)` - the extension's own mailbox, so the message waits in that line's box - and hears the unavailable greeting.
3. The message is stored in the private voicemail volume.
4. Compatible SIP phones receive message-waiting indication through the endpoint mailbox subscription.

This behavior applies to direct extension calls, specifically routed DIDs, and the inbound fallback extension. Extensions without voicemail enabled simply hang up after the ring timeout.

## Phone access

From a registered extension, dial:

```text
*97
```

Set the IP phone's **Voicemail number / Message key number / Voicemail access
code** to `*97`, **not** the mailbox PIN. These phone fields usually contain a
number to dial, not a password. If the phone has a separate voicemail password
field, it is optional; manual PIN entry after connection is supported.

The authenticated endpoint supplies its own number-scoped mailbox to
`VoiceMailMain` in the private `engineerip` context. Enter only the configured
mailbox PIN when prompted, optionally followed by `#`. There is no mailbox-number
prompt, and callers never need to type a hyphenated mailbox name on the keypad.
PIN authentication is not skipped. An endpoint without a configured mailbox is
refused rather than offered a generic login to other mailboxes.

Set or reset the PIN in the extension's call handling (4–10 digits), save the
workflow, and ensure the PBX configuration reload succeeds. This updates the
same encrypted PIN as extension settings. Leaving the field blank preserves the
existing PIN; the console does not read the saved PIN back. Phone and browser
endpoints, their SIP-username aliases, and message-waiting notifications use the
same mailbox identity. The endpoint advertises `*97` as its voicemail destination
to compatible message-waiting clients.

## Administration panel

The **Voicemails** sidebar page provides:

- mailbox/extension and employee-name grouping;
- New, Read, and Urgent folder filters;
- caller, received time, duration, and message status;
- secure browser audio playback;
- mark-new-message-as-read;
- permanent deletion;
- filtering by extension and caller.

Deleting an extension is blocked while it still has voicemail messages. Delete or otherwise retain the messages according to company policy first.

## API authentication

Every endpoint requires:

```http
Authorization: Bearer <TELEPHONY_TOKEN>
```

### List mailboxes and counts

```http
GET /api/v1/voicemail/mailboxes
Authorization: Bearer <TELEPHONY_TOKEN>
```

Returns enabled extensions with display names, active state, and New, Old, Urgent, and Total counts. PINs are never returned.

### List messages

```http
GET /api/v1/voicemails
GET /api/v1/voicemails?extension=101-13025550001
GET /api/v1/voicemails?extension=101@+13025550001
GET /api/v1/voicemails?extension=101                     (while one extension of the key holds the digits)
GET /api/v1/voicemails?extension=101&folder=inbox
```

Folders are `inbox`, `old`, and `urgent`.

Example response:

```json
{
  "total": 1,
  "voicemails": [
    {
      "id": "101:inbox:msg0000",
      "mailbox": "101-13025550001",
      "folder": "inbox",
      "message": "msg0000",
      "caller_id": "Customer <+13025550123>",
      "caller_channel": "PJSIP/provider-000001",
      "duration_seconds": 42,
      "received_at": "2026-09-18T12:00:00+00:00",
      "format": "wav",
      "size_bytes": 68044
    }
  ]
}
```

### Play or download audio

```http
GET /api/v1/voicemails/101-13025550001/inbox/msg0000/file
Authorization: Bearer <TELEPHONY_TOKEN>
Range: bytes=0-
```

The endpoint supports conditional/range responses through Flask and does not expose a host filesystem path.

### Mark as read

```http
POST /api/v1/voicemails/101-13025550001/inbox/msg0000/read
Authorization: Bearer <TELEPHONY_TOKEN>
```

This moves a message from `INBOX` to Asterisk's `Old` folder and safely renumbers mailbox files.

### Delete

```http
DELETE /api/v1/voicemails/101-13025550001/old/msg0000
Authorization: Bearer <TELEPHONY_TOKEN>
```

Deletion is permanent and removes the message metadata and all audio-format files.

## Storage and permissions

Compose creates the `asterisk_voicemail` named volume. It is mounted at:

- `/var/spool/asterisk/voicemail` in Asterisk;
- `/app/voicemail` in the API and ARI-worker containers.

Asterisk runs with the shared `telephony` group, allowing the unprivileged API process to manage only the dedicated voicemail volume. The volume is never published as a web directory. API/admin authentication is checked before resolving an audio file.

Back up `asterisk_voicemail` with `telephony_data`. Do not use `docker compose down -v` unless permanent loss of messages and settings is acceptable.

## CRM integration guidance

Store the Asterisk message identity (`mailbox`, `folder`, and `message`) only as a short-lived reference because marking read or deletion can renumber files. A CRM should refresh the list after every mutation. Use the authenticated file endpoint rather than constructing filesystem paths.

## Troubleshooting

### Caller hangs up without voicemail

- Confirm voicemail is enabled for the destination extension.
- Confirm a PIN was saved.
- Run `asterisk -rx 'voicemail show users'` in the Asterisk container.
- Inspect `voicemail.dynamic.conf` in the shared dynamic configuration volume.
- Check ARI-worker logs for AMI reload errors.

### `*97` cannot find the mailbox

Confirm the mailbox is enabled and rendered in `voicemail.dynamic.conf`, and that
the phone's authenticated endpoint has `set_var=ENGINEERIP_MAILBOX=<mailbox>`.
Dial `*97` from that endpoint; it should ask for the PIN only. Save a fresh PIN in
call handling if necessary, and check the configuration reload result.

For **“Address incomplete” / SIP 484**, first check that the phone's voicemail
number is `*97`, not its PIN or a mailbox name. Try dialing `*97` directly. If no
INVITE reaches Asterisk, the phone may have a local star-code or dial-plan rule;
allow `*97` or configure its Message key to dial `*97` on the correct registered
account. Check SIP logs to distinguish a local phone rejection from a server
response. Never post PINs, SIP passwords, or unredacted authentication traces in
support logs. After deploying a server fix, reload PJSIP, voicemail and dialplan
configuration using the normal configuration-sync process; restart/re-register
the phone if it caches the old message-waiting destination.

### Messages exist but admin playback returns 404

Check the `asterisk_voicemail` mounts, shared group permissions, message audio file format, and API container logs.

### No message-waiting light

Not every softphone supports the same MWI behavior. Confirm the endpoint has
`mailboxes=<mailbox>@engineerip` - the mailbox name, so `101-13025550001` for a device
that belongs to a line - subscription support is enabled, and the device is
subscribed/registered.
