#!/usr/bin/env python3
"""Generate the original, royalty-free instrumental loop shipped with the PBX.

The 8 kHz mono PCM WAV is playable by Asterisk's format_wav and browsers. Run
from the repository root to reproduce the asset; no external audio is used.
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 8000
BEAT = 0.6
# Four bars of a gentle original arpeggio. MIDI note numbers, not samples.
CHORDS = [(48, 55, 60, 64, 67, 71, 67, 64),
          (45, 52, 57, 60, 64, 69, 64, 60),
          (41, 48, 53, 57, 60, 64, 60, 57),
          (43, 50, 55, 59, 62, 67, 62, 59)]


def frequency(note: int) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def synthesize() -> bytes:
    duration = len(CHORDS) * 4 * BEAT
    data = bytearray()
    for sample in range(round(duration * RATE)):
        time = sample / RATE
        eighth = time / (BEAT / 2)
        note_index = int(eighth)
        chord = CHORDS[(note_index // 8) % len(CHORDS)]
        note = chord[note_index % 8]
        age = time - note_index * BEAT / 2
        # Short bell-like notes, with a softer sustained bass on each beat.
        envelope = min(1.0, age / 0.012) * math.exp(-age * 5.0)
        pitch = frequency(note)
        melody = envelope * (math.sin(2 * math.pi * pitch * age)
                             + 0.25 * math.sin(4 * math.pi * pitch * age))
        beat_age = time - int(time / BEAT) * BEAT
        bass_env = min(1.0, beat_age / 0.025) * math.exp(-beat_age * 3.0)
        bass = bass_env * math.sin(2 * math.pi * frequency(chord[0]) * beat_age)
        # Taper the loop boundaries so repeat playback has no audible click.
        fade = min(1.0, time / 0.04, (duration - time) / 0.04)
        value = max(-1.0, min(1.0, (0.30 * melody + 0.12 * bass) * fade))
        data.extend(struct.pack('<h', round(value * 32767)))
    return bytes(data)


def main() -> None:
    path = Path(__file__).resolve().parents[1] / 'asterisk' / 'moh' / 'engineerip-hold.wav'
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(synthesize())
    print(f'Generated {path} ({path.stat().st_size} bytes)')


if __name__ == '__main__':
    main()
