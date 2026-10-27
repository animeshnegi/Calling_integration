"""Offline voicemail speech recognition; no HTTP, model downloads or API keys.

The parent stays small. PocketSphinx and its bundled English model are loaded
only in a disposable, low-priority subprocess, never in the ARI event handler.
This file is also a standalone worker so Python does not import app/__init__.py
(and the entire web/telephony stack) in the speech-recognition subprocess.
"""
from __future__ import annotations

import array
import io
import json
import os
import signal
import subprocess
import sys
import wave
from pathlib import Path

MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_AUDIO_SECONDS = 300
WORKER_TIMEOUT_SECONDS = 120
WORKER_MEMORY_BYTES = 192 * 1024 * 1024
MAX_TRANSCRIPT_CHARACTERS = 20000
_FAILURE_STATUSES = {"unsupported_audio", "audio_too_long", "empty_transcript",
                     "engine_unavailable", "resource_limit", "worker_error"}


def transcribe(audio_bytes: bytes, suffix: str) -> tuple[str | None, str]:
    """Run the packaged offline recognizer. A failure never prevents email."""
    if suffix.lower() != ".wav" or len(audio_bytes) > MAX_AUDIO_BYTES:
        return None, "unsupported_audio"
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
            input=audio_bytes, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=WORKER_TIMEOUT_SECONDS, check=False,
            env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the worker on timeout.
        return None, "timeout"
    except OSError:
        return None, "engine_unavailable"
    if result.returncode != 0:
        sigkill = -getattr(signal, "SIGKILL", 9)
        sigxcpu = -getattr(signal, "SIGXCPU", signal.SIGTERM)
        limited = result.returncode in (sigkill, sigxcpu)
        return None, "resource_limit" if limited else "worker_error"
    try:
        if len(result.stdout) > 128 * 1024:
            return None, "worker_error"
        output = json.loads(result.stdout)
        status, text = output["status"], output.get("text")
        if status == "transcribed" and isinstance(text, str) and text.strip():
            return text.strip()[:MAX_TRANSCRIPT_CHARACTERS], status
        if status in _FAILURE_STATUSES:
            return None, status
    except (ValueError, TypeError, KeyError):
        pass
    return None, "worker_error"


def _limit_worker():
    if os.name == "posix":
        import resource
        # Avoid core files containing private audio/transcripts. Bound memory
        # and CPU independently of the parent ARI worker. No preexec_fn in a
        # threaded parent (which can deadlock); set limits in the child itself.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (60, 61))
        try:
            os.nice(15)
        except OSError:
            # Hardened containers may forbid renicing; recognition still works.
            pass


def _pcm16k_chunks(reader):
    """Stream standard Asterisk 8 kHz PCM as 16 kHz for the bundled model.

    Python 3.13 has no audioop. Linear interpolation needs neither ffmpeg nor
    numpy; keep a single carry sample so chunk boundaries are continuous.
    Input/output WAV PCM is little-endian even on big-endian machines.
    """
    previous = None
    while data := reader.readframes(4096):
        if reader.getframerate() == 16000:
            yield data
            continue
        samples = array.array("h")
        samples.frombytes(data)
        if sys.byteorder != "little":
            samples.byteswap()
        if previous is not None:
            samples.insert(0, previous)
        output = array.array("h")
        for left, right in zip(samples, samples[1:]):
            output.append(left)
            output.append((left + right) // 2)
        previous = samples[-1]
        if sys.byteorder != "little":
            output.byteswap()
        yield output.tobytes()
    if previous is not None:
        output = array.array("h", [previous, previous])
        if sys.byteorder != "little":
            output.byteswap()
        yield output.tobytes()


def _decode(audio_bytes: bytes) -> tuple[str | None, str]:
    try:
        source = io.BytesIO(audio_bytes)
        reader = wave.open(source, "rb")
    except (wave.Error, EOFError):
        return None, "unsupported_audio"
    with reader:
        if (reader.getcomptype() != "NONE" or reader.getnchannels() != 1
                or reader.getsampwidth() != 2 or reader.getframerate() not in (8000, 16000)):
            return None, "unsupported_audio"
        if reader.getnframes() > reader.getframerate() * MAX_AUDIO_SECONDS:
            return None, "audio_too_long"
        # A truncated WAV must not be treated as a complete recording.
        if source.tell() + reader.getnframes() * 2 > len(audio_bytes):
            return None, "unsupported_audio"
        from pocketsphinx import Decoder, Endpointer
        decoder = Decoder(loglevel="FATAL")  # Packaged model; no network lookup.
        endpoint = Endpointer(sample_rate=16000)
        texts, pending = [], b""
        active, utterance_samples = False, 0

        def finish():
            nonlocal active, utterance_samples
            decoder.end_utt()
            hypothesis = decoder.hyp()
            if hypothesis and hypothesis.hypstr:
                texts.append(hypothesis.hypstr)
            active, utterance_samples = False, 0

        def consume(speech):
            nonlocal active, utterance_samples
            if speech:
                if not active:
                    decoder.start_utt()
                    active = True
                decoder.process_raw(speech, False, False)
                utterance_samples += len(speech) // 2
                # VAD separates phrases, reducing silence work and decoder RAM.
                # Also bound a single no-pause phrase to 30 seconds of history.
                if not endpoint.in_speech or utterance_samples >= 30 * 16000:
                    finish()

        for chunk in _pcm16k_chunks(reader):
            pending += chunk
            while len(pending) >= endpoint.frame_bytes:
                frame, pending = pending[:endpoint.frame_bytes], pending[endpoint.frame_bytes:]
                consume(endpoint.process(frame))
        if endpoint.in_speech:
            consume(endpoint.end_stream(pending))
        if active:
            finish()
        text = " ".join(texts).strip()[:MAX_TRANSCRIPT_CHARACTERS]
        return (text, "transcribed") if text else (None, "empty_transcript")


def _worker_main():
    try:
        _limit_worker()
        data = sys.stdin.buffer.read(MAX_AUDIO_BYTES + 1)
        text, status = _decode(data) if len(data) <= MAX_AUDIO_BYTES else (None, "unsupported_audio")
    except ImportError:
        text, status = None, "engine_unavailable"
    except MemoryError:
        text, status = None, "resource_limit"
    except Exception:
        # Never print decoder exceptions or private recording content.
        text, status = None, "worker_error"
    print(json.dumps({"text": text, "status": status}))


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker_main()
