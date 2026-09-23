# Voicemail Management

EngineerIP uses Asterisk `app_voicemail` with one mailbox per enabled extension. Mailbox definitions, PINs, message-waiting indicators, unanswered-call routing, browser playback, and CRM-facing APIs are integrated with the administration system.

## Enable a mailbox

1. Open `/admin` and select **Extensions**.
2. Add or edit an extension.
3. Enable **Voicemail** and enter a 4–10 digit numeric PIN. Leaving the PIN blank on a later edit keeps the existing encrypted PIN.
4. Save. The service atomically renders `voicemail.dynamic.conf`, updates the dialplan, and reloads PJSIP, voicemail, and dialplan modules through private AMI.

PINs are encrypted in `settings.db` and are never returned by the API or admin state. New messages can also be sent as SendGrid email attachments; see [`EMAIL_AND_USERS.md`](EMAIL_AND_USERS.md).

## Call behavior

For an extension with voicemail enabled:

1. Asterisk rings the SIP endpoint for 30 seconds.
2. If it is not answered, the caller enters `VoiceMail(<extension>@engineerip,u)` and hears the unavailable greeting.
3. The message is stored in the private voicemail volume.
4. Compatible SIP phones receive message-waiting indication through the endpoint mailbox subscription.

This behavior applies to direct extension calls, specifically routed DIDs, and the inbound fallback extension. Extensions without voicemail enabled simply hang up after the ring timeout.

## Phone access

From a registered extension, dial:

```text
*97
```

Asterisk opens `VoiceMailMain` in the private `engineerip` context. Enter the three-digit mailbox number and configured voicemail PIN when prompted. Users can record greetings and manage messages with Asterisk's voice menus.

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
GET /api/v1/voicemails?extension=101
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
      "mailbox": "101",
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
GET /api/v1/voicemails/101/inbox/msg0000/file
Authorization: Bearer <TELEPHONY_TOKEN>
Range: bytes=0-
```

The endpoint supports conditional/range responses through Flask and does not expose a host filesystem path.

### Mark as read

```http
POST /api/v1/voicemails/101/inbox/msg0000/read
Authorization: Bearer <TELEPHONY_TOKEN>
```

This moves a message from `INBOX` to Asterisk's `Old` folder and safely renumbers mailbox files.

### Delete

```http
DELETE /api/v1/voicemails/101/old/msg0000
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

Confirm the mailbox is enabled and rendered in `voicemail.dynamic.conf`. At the prompt, enter the three-digit extension mailbox and its PIN.

### Messages exist but admin playback returns 404

Check the `asterisk_voicemail` mounts, shared group permissions, message audio file format, and API container logs.

### No message-waiting light

Not every softphone supports the same MWI behavior. Confirm the endpoint has `mailboxes=<extension>@engineerip`, subscription support is enabled, and the device is subscribed/registered.
