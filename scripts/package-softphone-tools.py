#!/usr/bin/env python3
"""Build a small host-tools archive; no Docker build, Git pull or secrets needed.

Run on the workstation and transfer the archive to the VM alongside (or instead
of rebuilding) the Docker images. Its root layout matches /opt/Calling_integration.
"""
import argparse
import hashlib
import os
from pathlib import Path
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "scripts/install-softphone-proxy.py",
    "scripts/check-softphone-websocket.py",
    "deploy/nginx/softphone-websocket.conf",
    "deploy/SOFTPHONE_TOOLS.md",
)


def package(output, root=ROOT):
    output = Path(output).resolve()
    root = Path(root).resolve()
    for name in FILES:
        source = root / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Missing or symlinked release file: {name}")
        if output == source.resolve():
            raise ValueError("Archive must not overwrite a source file")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=output.name + ".", dir=output.parent)
    os.close(fd)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for name in FILES:
                source = root / name
                info = archive.gettarinfo(str(source), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o644  # Invoke Python explicitly; no executable-bit dependency.
                with source.open("rb") as handle:
                    archive.addfile(info, handle)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_name(output.name + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    return output, checksum


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="softphone-proxy-tools.tar.gz")
    args = parser.parse_args(argv)
    try:
        output, checksum = package(args.output)
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.exit(1, f"Packaging failed: {exc}\n")
    print(f"Created {output}\nChecksum: {checksum}\nOnly the installer, checker, nginx snippet and instructions are included; no .env, images or Compose file.")


if __name__ == "__main__":
    main()
