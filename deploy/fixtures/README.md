# Offline speech self-test fixture

`goforward-8k.wav` is the PocketSphinx public “go forward ten meters” sample,
converted from 16 kHz raw signed little-endian PCM to 8 kHz mono WAV, matching
Asterisk voicemail. It contains no customer audio.

Source: https://github.com/cmusphinx/pocketsphinx/blob/v5.1.1/test/data/goforward.raw

Copyright Carnegie Mellon University; the upstream licence is reproduced in
`deploy/THIRD_PARTY_NOTICES.md` and copied into the API image. The sample is
also used by the offline regression tests and by
`scripts/check-voicemail-transcription.py`.
