"""The PBX and browser ship the same non-empty, usable hold music."""
from pathlib import Path
import importlib.util
import math
import struct
import wave


ROOT = Path(__file__).resolve().parents[1]
MUSIC = ROOT / "asterisk" / "moh" / "engineerip-hold.wav"


def test_hold_music_is_original_playable_mono_and_reproducible():
    spec = importlib.util.spec_from_file_location("generate_hold_music", ROOT / "tools" / "generate-hold-music.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    with wave.open(str(MUSIC), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 8000)
        assert 8 <= audio.getnframes() / audio.getframerate() <= 30
        samples = audio.readframes(audio.getnframes())
    assert samples == generator.synthesize()
    values = [x[0] for x in struct.iter_unpack("<h", samples)]
    rms = math.sqrt(sum(value * value for value in values) / len(values))
    assert 1000 < rms < 20000  # an audible song, not silence or clipping
    assert max(abs(value) for value in values) < 32767
    assert abs(values[0]) < 20 and abs(values[-1]) < 20  # loop without clicks


def test_music_and_required_modules_are_packaged_outside_persistent_volume():
    config = (ROOT / "asterisk" / "config" / "musiconhold.conf").read_text()
    assert "[default]" in config and "mode=files" in config
    assert "directory=/usr/share/asterisk/engineerip-moh" in config
    asterisk_image = (ROOT / "asterisk" / "Dockerfile").read_text()
    assert "COPY moh/ /usr/share/asterisk/engineerip-moh/" in asterisk_image
    assert "res_musiconhold format_wav" in asterisk_image
    api_image = (ROOT / "Dockerfile").read_text()
    assert "COPY --chown=appuser:appuser asterisk/moh ./asterisk/moh" in api_image
