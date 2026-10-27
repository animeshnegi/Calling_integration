#!/usr/bin/env python3
"""Verify the bundled offline voicemail recognizer with a public WAV sample.

Run inside the imported API image (no network needed):

    docker run --rm --network none IMAGE python \
        /app/deploy-kit/scripts/check-voicemail-transcription.py

It also works from a source checkout by running:

    python3 scripts/check-voicemail-transcription.py

A deployment script can bind-mount the corrected host script and sample into
an older image and pass `--sample /tmp/goforward-8k.wav`. Only the public
sample transcript is printed. No SendGrid key, voicemail, customer data, or
application database is read.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
CANDIDATES = (Path.cwd(), *HERE.parents)
SAMPLE_PATHS = (
    "deploy/fixtures/goforward-8k.wav",                 # Source checkout.
    "deploy-kit/deploy/fixtures/goforward-8k.wav",     # Actual /app/deploy-kit layout in the image.
    "deploy-kit/fixtures/goforward-8k.wav",            # Backwards-compatible older kit.
)


def find_app_root() -> Path | None:
    return next((base for base in CANDIDATES if (base / "app/local_transcription.py").is_file()), None)


def find_sample(root: Path) -> Path | None:
    return next((path for name in SAMPLE_PATHS if (path := root / name).is_file()), None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", help="explicit WAV path, e.g. a host file bind-mounted into an older image")
    args = parser.parse_args(argv)
    root = find_app_root()
    if root is None:
        print(json.dumps({"status": "engine_unavailable", "error": "app/local_transcription.py not found"}))
        print("FAIL: run this inside the imported API image or updated source checkout.", file=sys.stderr)
        return 1
    sample = Path(args.sample) if args.sample else find_sample(root)
    if sample is None or not sample.is_file():
        print(json.dumps({"status": "engine_unavailable", "error": "bundled speech sample not found"}))
        print("FAIL: speech sample is missing; extract the tools archive or pass --sample PATH.", file=sys.stderr)
        return 1
    sys.path.insert(0, str(root))
    from app.local_transcription import transcribe

    text, status = transcribe(sample.read_bytes(), ".wav")
    ok = status == "transcribed" and bool(text) and "go forward" in text.lower()
    print(json.dumps({"status": status, "transcript": text, "offline": True}))
    print("PASS: bundled CPU recognizer decoded 8 kHz WAV offline." if ok
          else f"FAIL: local transcription status is {status}.", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
