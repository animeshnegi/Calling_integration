#!/usr/bin/env python3
"""Install the SIP /ws route into a running Docker nginx/OpenResty HTTPS vhost.

Dry run by default. Run on the Docker/1Panel VM, not inside the application
container. --apply requires root and backs up the persistent vhost file before
editing. Does not restart Asterisk, change SIP credentials or place calls.
"""
import argparse
import datetime
import fcntl
import importlib.util
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sip_ws_check", ROOT / "scripts/check-softphone-websocket.py")
_checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_checker)
check_websocket = _checker.check_websocket
MARKER = "EngineerIP managed SIP WebSocket route"


class InstallError(RuntimeError):
    pass


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=25)
    if result.returncode:
        # Never dump nginx -T output or Docker environments: they may contain secrets.
        detail = result.stderr.strip()[:600] if "-T" not in args else "configuration dump failed; inspect the proxy's config locally"
        raise InstallError(f"Command failed ({result.returncode}): {args[0]}: {detail}")
    return result.stdout


@dataclass
class Token:
    value: str
    start: int
    end: int
    punctuation: bool = False


def tokens(text):
    """Tokenize nginx syntax without mistaking quoted/variable braces for blocks."""
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        if text[i] == "#":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end + 1
            continue
        start = i
        if text[i] in "{};":
            i += 1
            yield Token(text[start:i], start, i, True)
            continue
        value = ""
        while i < len(text) and not text[i].isspace() and text[i] not in "{};#":
            if text[i] in "\"'":
                quote = text[i]
                i += 1
                while i < len(text) and text[i] != quote:
                    if text[i] == "\\" and i + 1 < len(text):
                        i += 1
                    value += text[i]
                    i += 1
                if i == len(text):
                    raise InstallError("Unterminated quoted nginx token; no changes made")
                i += 1
            elif text[i:i + 2] == "${":
                end = text.find("}", i + 2)
                if end < 0:
                    raise InstallError("Unterminated nginx variable; no changes made")
                value += text[i:end + 1]
                i = end + 1
            elif text[i] == "\\" and i + 1 < len(text):
                value += text[i + 1]
                i += 2
            else:
                value += text[i]
                i += 1
        if i == start:
            raise InstallError("Unsupported nginx token; no changes made")
        yield Token(value, start, i)


@dataclass
class Block:
    header: list
    start: int
    opening: int
    closing: int = -1
    end: int = -1
    parent: object = None
    directives: list = None


def blocks(text):
    found, stack, pending = [], [], []
    for token in tokens(text):
        if token.punctuation and token.value == "{":
            if not pending:
                raise InstallError("nginx block has no header")
            block = Block([t.value for t in pending], pending[0].start, token.end,
                          parent=stack[-1] if stack else None, directives=[])
            found.append(block)
            stack.append(block)
            pending = []
        elif token.punctuation and token.value == "}":
            if not stack or pending:
                raise InstallError("Unbalanced nginx configuration")
            block = stack.pop()
            block.closing, block.end = token.start, token.end
        elif token.punctuation and token.value == ";":
            if stack:
                stack[-1].directives.append([t.value for t in pending])
            pending = []
        else:
            pending.append(token)
    if stack or pending:
        raise InstallError("Incomplete nginx configuration")
    return found


def https_servers(text, domain):
    result = []
    for block in blocks(text):
        if block.header != ["server"]:
            continue
        names = [v for d in block.directives if d and d[0] == "server_name" for v in d[1:]]
        listens = [d[1:] for d in block.directives if d and d[0] == "listen"]
        tls = any("ssl" in d or any(v == "443" or v.endswith(":443") for v in d) for d in listens)
        if domain in names and tls:
            result.append(block)
    return result


def config_files(dump):
    files = {}
    matches = list(re.finditer(r"^# configuration file ([^\r\n]+):\s*$", dump, re.M))
    for i, match in enumerate(matches):
        files[match.group(1)] = dump[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(dump)]
    if not files:
        raise InstallError("Cannot identify nginx -T config files; no changes made")
    return files


def persistent_path(container_path, mounts):
    path = PurePosixPath(container_path)
    candidates = []
    for mount in mounts:
        destination = PurePosixPath(mount["Destination"])
        if path == destination or destination in path.parents:
            candidates.append((len(destination.parts), mount, path.relative_to(destination)))
    if not candidates:
        raise InstallError("Active vhost is not on a persistent Docker mount; configure it through 1Panel instead")
    _, mount, relative = max(candidates, key=lambda item: item[0])
    if not mount.get("RW", False):
        raise InstallError("Active vhost mount is read-only; no changes made")
    source = Path(mount["Source"]).resolve()
    resolved = (source / str(relative)).resolve() if str(relative) != "." else source
    if resolved != source and source not in resolved.parents:
        raise InstallError("Vhost symlink leaves its persistent mount; refusing to edit")
    if not resolved.is_file():
        raise InstallError("Persistent vhost file is unavailable on this host; run on the Docker VM")
    return resolved


def render_route(text, domain, upstream):
    servers = https_servers(text, domain)
    if len(servers) != 1:
        raise InstallError("Need exactly one matching HTTPS server in the selected vhost")
    server = servers[0]
    exact = [b for b in blocks(text) if b.parent and b.parent.start == server.start and b.header == ["location", "=", "/ws"]]
    route = (ROOT / "deploy/nginx/softphone-websocket.conf").read_text()
    route = route[route.index("location = /ws {"):].replace("http://127.0.0.1:8088", upstream)
    route = route.replace("location = /ws {\n", f"location = /ws {{\n    # {MARKER}\n", 1)
    indented = "\n".join("    " + line if line else "" for line in route.rstrip().splitlines())
    if exact:
        if len(exact) != 1 or MARKER not in text[exact[0].start:exact[0].end]:
            raise InstallError("An unmanaged exact /ws location already exists; inspect it in 1Panel rather than overwriting it")
        old = exact[0]
        # Keep the existing leading indentation, since block.start excludes it.
        return text[:old.start] + indented.lstrip() + text[old.end:]
    return text[:server.closing] + "\n" + indented + "\n" + text[server.closing:]


class Proxy:
    def __init__(self, name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise InstallError("Invalid proxy container name")
        self.name = name
        binary = self.exec("sh", "-c", "command -v nginx || command -v openresty").strip().splitlines()
        if len(binary) != 1 or not binary[0].startswith("/"):
            raise InstallError("Cannot identify the proxy's nginx/OpenResty executable")
        self.binary = binary[0]
        args = self.inspect(".Args")
        self.flags = []
        for flag in ("-p", "-c"):
            if flag in args:
                i = args.index(flag)
                if i + 1 >= len(args):
                    raise InstallError("Invalid proxy startup arguments")
                self.flags += [flag, args[i + 1]]

    def exec(self, *args):
        return command("docker", "exec", self.name, *args)

    def inspect(self, field):
        return json.loads(command("docker", "inspect", "--format", "{{json " + field + "}}", self.name))

    def nginx(self, option):
        return self.exec(self.binary, *self.flags, *option)


@dataclass
class Plan:
    path: Path
    original: str
    updated: str
    domain: str
    network: str
    attach: bool
    direct_url: str
    upstream: str


def make_plan(proxy, domain, asterisk="engineerip-asterisk", network="engineerip-telephony-internal"):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain) or "." not in domain:
        raise InstallError("Use a DNS hostname only (no scheme, port, path or shell characters)")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", asterisk):
        raise InstallError("Invalid Asterisk container name")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", network):
        raise InstallError("Invalid private network name")
    mode = proxy.inspect(".HostConfig.NetworkMode")
    if mode == "none" or mode.startswith("container:"):
        raise InstallError("Unsupported proxy network mode; inspect its namespace through 1Panel")
    peer_networks = json.loads(command("docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", asterisk))
    if mode == "host":
        upstream, direct, attach, shared = "http://127.0.0.1:8088", "ws://127.0.0.1:8088/ws", False, ""
    else:
        shared_networks = set(proxy.inspect(".NetworkSettings.Networks")) & set(peer_networks)
        shared = network if network in shared_networks else next(iter(sorted(shared_networks)), network)
        if shared not in peer_networks:
            raise InstallError("Asterisk is not attached to the selected private network")
        address = peer_networks[shared].get("IPAddress", "")
        ip = ipaddress.ip_address(address)
        if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_unspecified:
            raise InstallError("Expected Asterisk's private Docker IPv4 address for the direct preflight")
        upstream = f"http://{asterisk}:8088"
        direct, attach = f"ws://{ip}:8088/ws", shared not in shared_networks
    matches = []
    for filename, text in config_files(proxy.nginx(["-T"])).items():
        if https_servers(text, domain):
            matches.append(filename)
    if len(matches) != 1:
        raise InstallError("Cannot uniquely identify the active HTTPS vhost; no files changed. Check exact server_name/listen in 1Panel")
    path = persistent_path(matches[0], proxy.inspect(".Mounts"))
    original = path.read_text()
    return Plan(path, original, render_route(original, domain, upstream), domain, shared, attach, direct, upstream)


def write_locked(handle, content):
    handle.seek(0)
    handle.write(content)
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def apply_plan(proxy, plan, *, connect_network=False):
    ok, detail = check_websocket(plan.direct_url, origin=f"https://{plan.domain}")
    if not ok:
        raise InstallError("Asterisk direct WebSocket preflight FAILED; no config/network changed: " + detail)
    if plan.attach and not connect_network:
        raise InstallError("Proxy has no shared private network with Asterisk. Review --connect-network and persist that external network in the 1Panel proxy compose configuration")
    connected, backup = False, None
    try:
        if plan.attach:
            command("docker", "network", "connect", plan.network, proxy.name)
            connected = True
        # In-place write preserves single-file bind mounts; flock + content check
        # rejects concurrent edits. Backups are private and do not match *.conf.
        with plan.path.open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if handle.read() != plan.original:
                raise InstallError("Vhost changed since inspection; refusing to overwrite another edit")
            if plan.updated != plan.original:
                stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = plan.path.with_name(plan.path.name + f".engineerip-{stamp}.bak")
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as copy:
                    copy.write(plan.original)
                try:
                    write_locked(handle, plan.updated)
                    proxy.nginx(["-t"])
                    proxy.nginx(["-s", "reload"])
                except Exception as exc:
                    write_locked(handle, plan.original)
                    try:
                        proxy.nginx(["-t"])
                        proxy.nginx(["-s", "reload"])
                    except Exception:
                        raise InstallError("Original file restored, but proxy recovery validation/reload failed; inspect 1Panel locally") from exc
                    raise InstallError("Proxy configuration rejected; original vhost restored and reloaded") from exc
            else:
                proxy.nginx(["-t"])
    except Exception:
        if connected:
            command("docker", "network", "disconnect", plan.network, proxy.name)
        raise
    return backup


def discover_proxy():
    rows = [json.loads(line) for line in command("docker", "ps", "--format", "{{json .}}").splitlines() if line.strip()]
    candidates = [r["Names"] for r in rows if re.search(r"nginx|openresty", r.get("Image", "") + " " + r.get("Names", ""), re.I)]
    if len(candidates) != 1:
        raise InstallError("Pass --proxy-container NAME: could not uniquely identify the running nginx/OpenResty container")
    return candidates[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--proxy-container", help="auto-detect only when exactly one nginx/OpenResty container is running")
    parser.add_argument("--asterisk-container", default="engineerip-asterisk")
    parser.add_argument("--network", default="engineerip-telephony-internal")
    parser.add_argument("--apply", action="store_true", help="back up, edit, validate and reload the proxy; otherwise dry-run only")
    parser.add_argument("--connect-network", action="store_true", help="allow a live private network attachment if needed; persist it in 1Panel before recreating its proxy")
    args = parser.parse_args(argv)
    try:
        if args.apply and os.geteuid() != 0:
            raise InstallError("Run --apply with sudo on the Docker VM")
        proxy = Proxy(args.proxy_container or discover_proxy())
        plan = make_plan(proxy, args.domain, args.asterisk_container, args.network)
        print(f"Proxy: {proxy.name}\nPersistent vhost: {plan.path}\nExact route: /ws -> {plan.upstream}")
        if plan.attach:
            print(f"Private network attachment required: {plan.network}. Persist this external network in 1Panel's proxy compose before a future recreation.")
        if not args.apply:
            print("DRY RUN: no files or networks changed, no reload. Review, then run with --apply (and --connect-network if needed).")
            return 0
        backup = apply_plan(proxy, plan, connect_network=args.connect_network)
        print(f"Proxy route installed/validated. Backup: {backup or 'unchanged (already installed)'}")
        # Do not undo a valid installed route solely because an external CDN/DNS
        # or TLS check fails; report nonzero and retain the backup for inspection.
        deadline = time.monotonic() + 10
        while True:
            ok, detail = check_websocket(f"wss://{args.domain}/ws", timeout=3)
            if ok or time.monotonic() >= deadline:
                break
            time.sleep(.5)
        print(("PASS: " if ok else "NOT VERIFIED: route installed, but public check failed: ") + detail)
        return 0 if ok else 1
    except (InstallError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
