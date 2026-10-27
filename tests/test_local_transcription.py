"""Offline CPU recognizer, real bundled model, resource and privacy guards."""
import array
import io
import json
import signal
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import local_transcription as local

FIXTURE = Path(__file__).parent / "fixtures/local-transcription/goforward-8k.wav"
ROOT = Path(__file__).resolve().parents[1]


def wav(samples=b"\0\0" * 1600, rate=8000, channels=1, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(samples)
    return buffer.getvalue()


def test_real_bundled_model_recognizes_asterisk_rate_audio_offline():
    # This test runs the actual isolated worker with its 192 MiB memory limit,
    # the installed model and a public speech fixture, not a fake HTTP result.
    assert local.transcribe(FIXTURE.read_bytes(), ".wav") == ("go forward ten meters", "transcribed")


def test_real_worker_accepts_16k_pcm_without_resampling():
    with wave.open(str(FIXTURE), "rb") as audio:
        samples = array.array("h", audio.readframes(audio.getnframes()))
    doubled = array.array("h", (s for value in samples for s in (value, value)))
    transcript, status = local.transcribe(wav(doubled.tobytes(), rate=16000), ".WAV")
    assert status == "transcribed" and transcript.startswith("go forward ten")


def test_real_silence_does_not_invent_a_transcript():
    assert local.transcribe(wav(), ".wav") == (None, "empty_transcript")


@pytest.mark.parametrize("data", [b"not a WAV", b"RIFF", wav(channels=2), wav(width=1), wav(rate=44100), wav()[:-8]])
def test_real_worker_rejects_unsupported_or_truncated_audio(data):
    assert local.transcribe(data, ".wav") == (None, "unsupported_audio")


def test_legacy_gsm_never_starts_a_worker(monkeypatch):
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **kw: pytest.fail("Must not spawn for GSM"))
    assert local.transcribe(b"gsm audio", ".gsm") == (None, "unsupported_audio")


def test_rejects_excessive_duration_before_loading_model():
    # Standard RIFF header whose declared PCM frames exceed the 5 minute cap.
    import struct
    data = bytearray(wav())
    struct.pack_into("<I", data, 40, (local.MAX_AUDIO_SECONDS + 1) * 8000 * 2)
    assert local._decode(bytes(data)) == (None, "audio_too_long")


def test_8k_interpolation_is_continuous_across_chunk_boundaries():
    samples = array.array("h", range(5000))
    with wave.open(io.BytesIO(wav(samples.tobytes())), "rb") as audio:
        converted = array.array("h", b"".join(local._pcm16k_chunks(audio)))
    assert len(converted) == 2 * len(samples)
    assert converted[8190:8198].tolist() == [4095, 4095, 4096, 4096, 4097, 4097, 4098, 4098]
    assert converted[-2:].tolist() == [4999, 4999]


@pytest.mark.parametrize("failure,expected", [(subprocess.TimeoutExpired("worker", 120), "timeout"),
                                              (OSError("private recording details"), "engine_unavailable")])
def test_worker_timeout_and_spawn_failure_are_safe(monkeypatch, failure, expected):
    def fail(*a, **kw):
        raise failure
    monkeypatch.setattr(local.subprocess, "run", fail)
    assert local.transcribe(b"voice", ".wav") == (None, expected)


@pytest.mark.parametrize("code,status", [(-signal.SIGKILL, "resource_limit"), (-signal.SIGXCPU, "resource_limit"),
                                         (1, "worker_error"), (-signal.SIGABRT, "worker_error")])
def test_native_worker_failure_is_isolated(monkeypatch, code, status):
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=code, stdout=b""))
    assert local.transcribe(b"voice", ".wav") == (None, status)


@pytest.mark.parametrize("output", [b"bad-json", b"[]", b"null", b"{}", b'{"status":"transcribed","text":42}',
                                    b'{"status":"private internal error","text":"private transcript"}', b"x" * 140000])
def test_invalid_worker_output_cannot_leak_private_errors(monkeypatch, output):
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=output))
    assert local.transcribe(b"voice", ".wav") == (None, "worker_error")


def test_parent_uses_stdin_no_shell_no_audio_file_and_bounded_low_thread_worker(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "transcribed", "text": "hello"}).encode())
    monkeypatch.setattr(local.subprocess, "run", run)
    assert local.transcribe(b"private audio", ".wav") == ("hello", "transcribed")
    command, args = calls[0]
    assert command == [sys.executable, "-I", str(Path(local.__file__).resolve()), "--worker"]
    assert args["input"] == b"private audio" and args["timeout"] == 120
    assert args["stderr"] == subprocess.DEVNULL and not args.get("shell")
    assert args["env"]["OMP_NUM_THREADS"] == args["env"]["OPENBLAS_NUM_THREADS"] == "1"
    assert "preexec_fn" not in args


def test_worker_limits_cpu_memory_priority_and_core_dumps(monkeypatch):
    import resource
    limits, priorities = [], []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, value: limits.append((kind, value)))
    monkeypatch.setattr(local.os, "nice", priorities.append)
    local._limit_worker()
    assert (resource.RLIMIT_AS, (192 * 1024 * 1024, 192 * 1024 * 1024)) in limits
    assert (resource.RLIMIT_CPU, (60, 61)) in limits
    assert (resource.RLIMIT_CORE, (0, 0)) in limits
    assert priorities == [15]
    monkeypatch.setattr(local.os, "nice", lambda value: (_ for _ in ()).throw(OSError("EPERM")))
    local._limit_worker()  # Nice is best-effort; hard resource limits remain.


def test_deployment_bundles_offline_model_and_no_cloud_transcription_endpoint():
    assert "pocketsphinx==5.1.1" in (ROOT / "requirements.txt").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "Decoder(loglevel='FATAL')" in dockerfile
    assert "THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md" in dockerfile
    assert "Copyright (c) 1999-2016 Carnegie Mellon University" in (ROOT / "deploy/THIRD_PARTY_NOTICES.md").read_text()
    for filename in ("app/voicemail_email.py", "app/local_transcription.py", "web/admin.html", "web/admin.js"):
        source = (ROOT / filename).read_text()
        assert "api.openai.com" not in source and "whisper-1" not in source
    page = (ROOT / "web/admin.html").read_text()
    assert 'id="transcription-key"' not in page and "PocketSphinx English model" in page
