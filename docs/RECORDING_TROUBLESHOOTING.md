# Call recording: failures and recovery

This concerns **bridged call recordings**, not voicemail messages. Voicemail is
recorded by Asterisk's VoiceMail application in a separate persistent volume.

## Changes in this fix

- Mixing bridges request `proxy_media` so media stays inside Asterisk for the
  bridge recorder, instead of permitting native media optimizations.
- Legacy `slin16` settings are normalized to the Asterisk file-format name
  `sln16`. `slin16` is a codec name, not the recording file-format identifier.
- Recording/bridge notification failures cannot interrupt recording setup or
  downgrade a successful recording.
- A delayed failed-start response/failure event cannot overwrite an already
  finalized or deleted recording.
- The worker checks the most recent 200 ended failed recordings as well as
  unfinished recording states. Only positive stored-recording metadata from ARI
  promotes a failure to finalized; missing/unavailable ARI responses do not.
  It does not recreate missing audio or prove that a file contains audible speech.
- New failures carry a safe diagnostic (`recording_error`) without exposing raw
  upstream response bodies, passwords or filesystem paths in the console.
- Image builds assert the presence of required bridge/ARI recording modules.

## Rollout

Rebuild/redeploy **both** the API/ARI worker image and the Asterisk image from this
branch using your normal deployment procedure. Deploy during a maintenance window:
restarting Asterisk interrupts live calls. Update both HTTP and worker processes
so they share the additive `recording_error` schema change. Existing media volumes
must be preserved. **Do not run `docker compose down -v`.**

The changes apply to newly created bridges. The ARI worker's periodic reconciliation
can recover a false failure on an ended call if Asterisk still has its saved file.
A genuinely missing recording cannot be recovered by relabeling its database row.

## Diagnose an affected call

Replace `CALL_ID` below with the exact call ID from the recording card. These are
read-only checks; do not paste unfiltered logs containing customer phone numbers
or credentials into public issues.

```sh
docker compose logs --since=30m telephony-ari | grep -F 'CALL_ID'
docker compose logs --since=30m asterisk | grep -F 'CALL_ID'
docker compose exec asterisk asterisk -rx 'module show like recording'
docker compose exec asterisk asterisk -rx 'module show like format_wav'
docker compose exec asterisk asterisk -rx 'module show like bridge_softmix'
docker compose exec asterisk sh -c 'df -h /var/spool/asterisk/recording; df -i /var/spool/asterisk/recording; ls -ld /var/spool/asterisk/recording'
```

Check recorder write access **as the Asterisk runtime user**, not root:

```sh
docker compose exec --user asterisk:telephony asterisk sh -c \
  'test -w /var/spool/asterisk/recording && echo "recording directory writable"'
```

`test -w` is a basic permissions check, not proof that a filesystem can allocate
new data; inspect free space/inodes, mount health, and Asterisk's actual write error.

### Interpret the result

- **Storage/write error:** Inspect ownership, available space/inodes and read-only
  or remote-volume restrictions. The entrypoint normally gives the runtime user
  ownership of the recording volume. Do not fix this with world-writable modes.
- **Unsupported format:** Use `wav` for ordinary browser playback; verify the
  corresponding Asterisk format module is installed/loaded. Raw signed-linear
  and GSM formats are not reliably playable by browser audio controls.
- **No bridge audio:** Verify connected call legs, media/RTP reachability and
  the deployed proxy-media bridge configuration. A successful SIP call setup
  alone does not prove audio packets reached Asterisk.
- **401/403:** Verify private ARI credentials/permissions. Do not expose ARI
  publicly or paste its credentials into a ticket.
- **Conflict/409:** Inspect live/stored recording state before restarting a
  recorder. Do not overwrite or delete existing recordings to hide the error.
- **Timeout/network error:** The request may have succeeded at Asterisk even if
  the response was lost. Let reconciliation check for stored audio after the call
  ends; do not blindly start another recorder with the same name.

## Acceptance check

Enable recording globally and on the selected extension, use WAV, then place a
new call. Answer, speak from both ends, end the call, allow a worker reconciliation
cycle, and play/download the recording. Confirm audible content from both ends.
Check that disabled recording still creates no recorder and that recorder failure
does not drop the live conversation. Unit tests and image-module checks cannot
replace this deployment test.
