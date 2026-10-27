# Offline speech regression fixture

`goforward-8k.wav` is the PocketSphinx public “go forward ten meters” sample,
converted from 16 kHz raw signed little-endian PCM to 8 kHz mono WAV, matching
Asterisk voicemail. It contains no customer audio.

Source: https://github.com/cmusphinx/pocketsphinx/blob/v5.1.1/test/data/goforward.raw
Source SHA: 350b76a92902e0580728fe675ee50753a383ffae

Copyright Carnegie Mellon University; the upstream license is reproduced in
`LICENSE` and in `deploy/THIRD_PARTY_NOTICES.md` for the Docker image. This tiny fixture permits a real offline recognition test without
network access or a provider account.
