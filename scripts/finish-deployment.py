#!/usr/bin/env python3
"""One command after copying a built image to the VM.

After extracting softphone-proxy-tools.tar.gz into /opt/Calling_integration,
full deployment (maintenance window: recreates the Compose services):

    python3 scripts/finish-deployment.py latest tel.engineerip.com

No `tools_container`/`docker cp` step is required. The script uses the host
tools archive; only `--refresh-tools-from-image` copies /app/deploy-kit from
the loaded image. Checks only, without starting containers or changing nginx:

    python3 scripts/finish-deployment.py --verify-only

This is a Python entrypoint because Python tolerates Windows CRLF copies. It
normalizes extracted host scripts before running them. It never prints .env,
keys, nginx -T dumps, or full Docker environments.
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAG = "latest"
DEFAULT_DOMAIN = "tel.engineerip.com"
TEXT_SUFFIXES = {".sh", ".py", ".conf", ".md"}
DIRECT_WEBSOCKET = "ws://127.0.0.1:8088/ws"


def run(argv, *, capture=False, check=True, env=None, cwd=ROOT):
    argv = [str(part) for part in argv]
    result = subprocess.run(argv, cwd=str(cwd), env=env, text=True,
                            capture_output=capture, check=False)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        # Installer already suppresses nginx -T dumps; cap everything else.
        raise RuntimeError(f"Command failed ({result.returncode}): {argv[0]}: {detail[:1000]}")
    return result


def compose_env(tag):
    return {**os.environ, "IMAGE_TAG": tag}


def normalise_text_files():
    """Undo Windows CRLF in copied shell/Python/nginx/docs files."""
    for folder in (ROOT / "scripts", ROOT / "deploy"):
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix in TEXT_SUFFIXES:
                data = path.read_bytes()
                if b"\r\n" in data:
                    path.write_bytes(data.replace(b"\r\n", b"\n"))


def require_host_tools():
    required = (
        "scripts/install-softphone-proxy.py",
        "scripts/check-softphone-websocket.py",
        "scripts/check-voicemail-transcription.py",
        "deploy/fixtures/goforward-8k.wav",
        "deploy/RELEASE_CHECKLIST.md",
    )
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        raise RuntimeError(
            "Missing host tools: " + ", ".join(missing) +
            f". Extract softphone-proxy-tools.tar.gz into {ROOT} first; docker load alone does not copy host files."
        )


def load_image(archive):
    path = Path(archive).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"Image archive not found: {path}")
    checksum = path.with_name(path.name + ".sha256")
    if checksum.is_file():
        print(f"== Verifying {checksum.name} ==")
        run(["sha256sum", "-c", checksum.name], cwd=path.parent)
    print(f"== Loading image archive: {path} ==")
    run(["docker", "image", "load", "--input", str(path)])


def start_services(tag):
    print("== Starting/recreating services from the loaded image (no build) ==")
    # This must happen BEFORE the proxy installer: its preflight needs the
    # engineerip-asterisk container and the loopback 8088 listener.
    result = run(["docker", "compose", "up", "-d", "--no-build", "--force-recreate"],
                 env=compose_env(tag), capture=True)
    if result.stdout.strip():
        print(result.stdout.strip())


def extract_deploy_kit(image):
    print("== Refreshing host tools from /app/deploy-kit in the loaded image ==")
    created = run(["docker", "create", image], capture=True).stdout.strip()
    if not created:
        raise RuntimeError("docker create returned no container ID")
    try:
        result = run(["docker", "cp", f"{created}:/app/deploy-kit/.", str(ROOT)],
                     capture=True, check=False)
        if result.returncode:
            raise RuntimeError("This image has no /app/deploy-kit. Rebuild from the current source on Windows; do not build on the VM.")
    finally:
        # Never leave the stopped helper container behind, even after a failure.
        run(["docker", "rm", created], capture=True, check=False)
    for required in ("scripts/install-softphone-proxy.py", "scripts/check-softphone-websocket.py",
                     "scripts/check-voicemail-transcription.py", "deploy/RELEASE_CHECKLIST.md"):
        if not (ROOT / required).is_file():
            raise RuntimeError(f"Loaded image is missing {required} in /app/deploy-kit. Rebuild from the current source on Windows.")
    normalise_text_files()


def wait_for_direct_websocket(timeout=60):
    print("== Waiting for Asterisk's direct SIP WebSocket ==")
    deadline = time.monotonic() + timeout
    checker = ROOT / "scripts/check-softphone-websocket.py"
    while True:
        result = run([sys.executable, str(checker), DIRECT_WEBSOCKET], capture=True, check=False)
        if result.returncode == 0:
            print(result.stdout.strip())
            return
        if time.monotonic() >= deadline:
            detail = (result.stdout or result.stderr or "unknown error").strip()
            raise RuntimeError(f"Asterisk is not answering {DIRECT_WEBSOCKET}: {detail[:500]}")
        time.sleep(2)


def install_proxy(domain, proxy_container, skip_proxy):
    if skip_proxy:
        print("== Proxy installation skipped (--skip-proxy) ==")
        return
    installer = [sys.executable, str(ROOT / "scripts/install-softphone-proxy.py"), "--domain", domain]
    if proxy_container:
        installer += ["--proxy-container", proxy_container]
    print("== Reviewing the active 1Panel/OpenResty /ws route ==")
    dry = run(installer, capture=True)
    print(dry.stdout.strip())
    if "Private network attachment required" in dry.stdout:
        print("NOTE: persist that external network in 1Panel's proxy Compose so a later proxy recreation keeps it.")
    print("== Applying and validating the /ws route ==")
    apply_command = installer + ["--apply", "--connect-network"]
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        apply_command = ["sudo", *apply_command]
    applied = run(apply_command, capture=True)
    print(applied.stdout.strip())
    if applied.stderr.strip():
        print(applied.stderr.strip()[:500], file=sys.stderr)


def check_architecture(image):
    actual = run(["docker", "image", "inspect", image, "--format", "{{.Architecture}}"],
                 capture=True).stdout.strip()
    machine = platform.machine().lower()
    expected = {"x86_64": "amd64", "amd64": "amd64",
                "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    if actual != expected:
        raise RuntimeError(f"Image architecture is {actual}, VM needs {expected}. Rebuild/load the matching platform.")
    print(f"PASS: image architecture {actual} matches {machine}.")


def offline_transcription(image):
    # Use the CORRECTED script/sample from the host-tools archive. An already
    # loaded older image can still contain a self-test with the wrong sample
    # path; bind-mounting avoids requiring a full image rebuild just to verify.
    checker = ROOT / "scripts/check-voicemail-transcription.py"
    sample = ROOT / "deploy/fixtures/goforward-8k.wav"
    for path in (checker, sample):
        if not path.is_file():
            raise RuntimeError(f"Missing {path.relative_to(ROOT)}. Extract softphone-proxy-tools.tar.gz into {ROOT} first.")
    result = run([
        "docker", "run", "--rm", "--network", "none", "-w", "/app",
        "-v", f"{checker}:/tmp/check-voicemail-transcription.py:ro",
        "-v", f"{sample}:/tmp/goforward-8k.wav:ro",
        image, "python", "/tmp/check-voicemail-transcription.py",
        "--sample", "/tmp/goforward-8k.wav",
    ], capture=True)
    print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def public_websocket(domain):
    result = run([sys.executable, str(ROOT / "scripts/check-softphone-websocket.py"),
                  f"wss://{domain}/ws"], capture=True)
    print(result.stdout.strip())


def fresh_admin_ui(domain):
    request = urllib.request.Request(f"https://{domain}/admin-assets/admin.js", method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            cache_control = response.headers.get("Cache-Control", "")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot fetch admin.js through {domain}: {exc}") from exc
    if "no-store" not in cache_control.lower():
        raise RuntimeError(f"admin.js Cache-Control is {cache_control!r}; expected no-store. Hard-refresh and confirm the new API image.")
    print(f"PASS: admin.js is fresh ({cache_control}).")


def verify(tag, domain, image):
    print("== Verifying image, offline transcription, /ws, admin UI and services ==")
    check_architecture(image)
    offline_transcription(image)
    public_websocket(domain)
    fresh_admin_ui(domain)
    status = run(["docker", "compose", "ps"], env=compose_env(tag), capture=True)
    print(status.stdout.strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", default=DEFAULT_TAG, help=f"loaded image tag (default: {DEFAULT_TAG})")
    parser.add_argument("domain", nargs="?", default=DEFAULT_DOMAIN, help=f"public HTTPS host (default: {DEFAULT_DOMAIN})")
    parser.add_argument("--verify-only", action="store_true", help="run checks only; no service start or nginx changes")
    parser.add_argument("--skip-proxy", action="store_true", help="start and verify but do not modify the 1Panel/nginx route")
    parser.add_argument("--proxy-container", help="exact nginx/OpenResty container name when auto-detection is ambiguous")
    parser.add_argument("--image-tar", help="optional image .tar/.tar.gz to docker load before finishing")
    parser.add_argument("--refresh-tools-from-image", action="store_true",
                        help="optional: overwrite host tools with /app/deploy-kit from the loaded image")
    args = parser.parse_args(argv)
    image = f"engineerip/telephony-api:{args.tag}"
    try:
        normalise_text_files()
        require_host_tools()
        if args.image_tar:
            load_image(args.image_tar)
        if not args.verify_only:
            print("Use this in a maintenance window: services are recreated.\n")
            start_services(args.tag)
            if args.refresh_tools_from_image:
                extract_deploy_kit(image)
                normalise_text_files()
                require_host_tools()
            wait_for_direct_websocket()
            install_proxy(args.domain, args.proxy_container, args.skip_proxy)
        verify(args.tag, args.domain, image)
        print("\nPASS: deployment is ready. Hard-refresh the admin page once.")
        print("Still check the email provider test, a new voicemail transcript, and one 101↔105 call separately.")
        return 0
    except (RuntimeError, OSError, urllib.error.URLError) as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        print("Send this sanitized output with the exact inline error; never send keys, .env or raw nginx dumps.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
