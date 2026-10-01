# IVR prompt recordings

The phone menu plays one of these files, chosen by the voice the operator picks on the
IVR step. Asterisk looks them up as `sound:custom/<name>`, so a file dropped here as
`ivr-welcome.ulaw` is what `sound:custom/ivr-welcome` plays.

| Voice in the console | File to install |
| --- | --- |
| English (US) - platform voice | `ivr-welcome.ulaw` (or `.wav`) |
| English (UK) | `ivr-welcome-en-gb.ulaw` |
| Espanol (US) | `ivr-welcome-es-us.ulaw` |
| Francais (Canada) | `ivr-welcome-fr-ca.ulaw` |

Record 8 kHz, 16-bit, mono audio (Asterisk's native format is G.711 u-law, `.ulaw`; a
`.wav` file must be 8 kHz, 16-bit, mono). `ffmpeg -i greeting.wav -ar 8000 -ac 1 -acodec
pcm_mulaw ivr-welcome.ulaw` produces one from a normal recording.

Until a file is installed the menu is not silent: the service falls back to Asterisk's
stock `vm-enter-num-to-call` prompt ("please enter the number you wish to call"), and the
step's **Spoken prompt** text stays in the console as the script the recording should say.
