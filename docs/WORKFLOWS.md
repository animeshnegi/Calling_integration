# Incoming-call workflow behavior

## Which editor controls what?

**Number workflow** controls the incoming DID's journey: opening hours, IVR,
ordered ring steps, and an optional explicit voicemail/external-forward fallback.
**Extension call handling** controls that desk: ringing on/off, its ring timer,
and one no-answer action (own voicemail, another extension, external forward, or
end call). Editing incoming membership does not change an extension's owner,
SIP identity, primary number, or outbound caller ID.

Save the workflow and check that the Asterisk configuration applied successfully.
A mailbox node provisions a mailbox even when the separate extension voicemail
switch was previously off. A failed configuration reload must be resolved before
assuming that the deployed Asterisk dialplan contains the new mailbox.

## Ringing behavior

| Step | Incoming behavior |
| --- | --- |
| Extension | Ring only the named extension's registered endpoints. |
| Simultaneous / ring group | Ring the selected members together, not all account extensions. |
| Sequential | Ring the selected members one at a time, in the saved member order. |
| Multiple ring steps | Finish each step unanswered before starting the next step. |
| Ring device off | Skip that desk's ring and apply its no-answer handling when the journey ends. |

An extension's browser and desk phone can ring together: they are endpoints of
the **same** extension, not unrelated extensions. An answer cancels other legs.

New ring steps require explicit destination selection. Existing auto-provisioned
number workflows can already contain every desk on that line: inspect their
selected destinations and remove unwanted members. Adding a step does not remove
existing steps. Cross-line desks ring only when explicitly assigned to the same
customer's workflow/ring sources; another customer's extensions are not allowed.

### Timers

- Number/ring-step timeout and a configured extension timeout are both limits;
  **the shorter limit wins**. Number 25s + desk 60s = 25s; number 60s + desk 25s = 25s.
- To ring for 40s, set both applicable limits to at least 40s.
- Sequential timeout is **per member**, not the duration of the whole sequence.
  Two desks at 25s can take approximately 50s before voicemail.
- Busy, rejected and unregistered endpoints can fail immediately; a timeout is a
  maximum wait, not a guaranteed minimum ring duration.
- Timers are passed to Asterisk originate; call setup/network latency can affect
  the exact audible duration.
- The remaining ring stages and no-answer destination are persisted with the
  call. Editing a workflow changes **new** calls, not calls already ringing.

## Which voicemail receives the call?

### Incoming call without an IVR selection

Example: number workflow rings `[101, 102, 103]`, and every desk has voicemail.
After the selected ring stages finish unanswered:

1. An explicit number-level voicemail/forward ending wins.
2. Otherwise the **first configured destination** supplies the shared no-answer
   policy. With the example above, it is 101's mailbox if 101's action is voicemail.
3. If that desk explicitly says end call, voicemail is not inferred from a later
   desk merely because that later desk has its own mailbox.

"First" means the saved workflow destination order, not whichever device stops
ringing first or necessarily the lowest extension number. To remove ambiguity,
add an explicit number-level voicemail node naming the required mailbox.

### Incoming call with IVR

A valid mapped key or full extension selection leaves the number's menu:

- Only the selected desk rings (including that desk's registered endpoints).
- The selected desk's timer, ring switch and no-answer policy apply.
- If the selected desk has own-voicemail handling, its own mailbox receives the
  message, **not the number workflow's default mailbox**.
- A selected desk configured to forward or end the call does that instead.

The number's fallback extension or following ring/voicemail/forward steps apply
when the caller never enters a valid selection and exhausts the configured
attempts. The input timeout begins after the greeting finishes. Entering a digit
stops the greeting and starts a full configured timeout between digits (there
is no separate hard-coded short inter-digit timer). A delayed finish event for
the interrupted greeting cannot extend that wait. Each retry starts a new
attempt; mapped destinations still dial immediately when a valid key is entered.
A missing playback-finished event has a bounded 45s extra safety wait rather
than leaving the caller stuck forever.

Forwarding to another extension starts a new ring step: its configured forward
ring duration is capped by the destination desk's own timer, not the original
number step's elapsed/shorter limit. Both extension and external forwarding
ring durations can be edited in Extension Call Handling and survive subsequent
saves. The default forwarding duration is 25 seconds.

### Mailbox identity

Mailboxes are number-scoped. `101` on two different numbers means two different
mailboxes. A stored key such as `101@+13025550001` maps to the Asterisk mailbox
`101-13025550001`; the workflow must not borrow the other line's 101.

## Other menu items and limitations

- **Business hours:** Gate the entire number journey even if the tile appears
  after a ring tile. Closed calls skip IVR/ringing and use the configured ending.
  Opening is inclusive; closing is exclusive. Overnight shifts are supported,
  with the after-midnight portion belonging to the opening weekday. Times use
  the application server's local timezone; configure that deployment timezone
  deliberately. No per-workflow timezone selector is implemented.
- **Forward to external number:** Uses its own forward timeout, including after
  hours. Carrier authorization, caller-ID policy and provider availability still
  apply. Unanswered external forwarding is not recursively retried.
- **Forward to another extension:** Gives the target one chance; no recursive
  extension-forwarding chain is followed. Explicit number endings remain in
  force; otherwise the target's handling can supply the ending.
- **Disabled IVR/desk workflows:** Do not override active routing. Deactivating a
  number workflow falls back to the DID's inbound extension rather than disabling
  the DID itself; deactivate the number to stop routing that DID.
- **IVR prompt text/voice:** Text is a recording script, **not text-to-speech**.
  Install the matching audio described in
  [the prompt-recording guide](../asterisk/sounds/custom/README.md). Missing custom
  audio falls back to Asterisk's stock extension prompt; arbitrary key-mapping
  announcements require a matching recording.
- This guide describes external inbound DID/ARI workflows. Direct SIP extension
  dialing uses its generated dialplan; do not assume a number's IVR or number-level
  timer also applies to a direct internal-extension call.

## Verification and release checks

`tests/test_routing_studio.py` covers ring modes/order, first-desk voicemail,
IVR-selected voicemail/forwarding, timer precedence, ordered stages after worker
recreation, configuration-edit isolation, mailbox/dialplan provisioning, disabled
policies, business hours/overnight hours, after-hours forwarding and IVR greeting
timing. The wider service and telephony tests cover answered-leg cancellation,
recording, outbound behavior, cross-tenant restrictions and existing forwarding.

These are deterministic application/fake-ARI and generated-configuration checks,
not proof of a live carrier deployment. On staging, call the DID from an outside
phone; measure ringing; choose each IVR destination; leave a message; retrieve it
from the correct mailbox; test busy/offline endpoints, answer cancellation, and
confirm no-answer forwarding. Check ARI worker and Asterisk logs if an expected
mailbox transfer fails. See [TESTING.md](TESTING.md) for test commands.

### Personal voicemail greetings

In **Voicemail → Personal voicemail greeting**, select an extension and upload
its own recorded greeting (0.1–120 seconds, source file at most 20 MB). The
browser converts supported audio formats to Asterisk-compatible mono 8 kHz,
16-bit PCM WAV. The server validates the audio and installs `unavail.wav`
atomically in that extension's number-scoped mailbox on the shared voicemail
volume. No Asterisk restart or configuration reload is required.

The preview plays the greeting callers hear before leaving a message, not the
system voice that reads mailbox menus. You can also dial `*97` from the desk,
enter its mailbox PIN, and use mailbox options to record its unavailable greeting.
A phone-recorded temporary greeting takes precedence; uploading a replacement
removes that temporary override.

No personal recording is required. **Use automated greeting** removes custom
unavailable, busy, temporary, and recorded-name greeting files to restore the
standard Asterisk greeting. It does not delete messages or change the mailbox
PIN, voicemail routing, or email settings. Voicemail must still be enabled in
the extension/number workflow. Tenant users can manage only mailboxes belonging
to their account; administrator sessions can manage any existing extension.
