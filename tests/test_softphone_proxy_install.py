"""Container-aware installation: safe vhost selection, preflight and rollback."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("proxy_install", ROOT / "scripts/install-softphone-proxy.py")
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
spec.loader.exec_module(installer)
DOMAIN = "tel.engineerip.com"
HOST_CONFIG = '''# Do not edit other domains or TLS directives.
server {
    listen 80;
    server_name tel.engineerip.com;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl;
    server_name tel.engineerip.com;
    ssl_certificate /certs/site.pem;
    ssl_certificate_key /certs/site.key;
    location / { proxy_pass http://127.0.0.1:5000; }
    location ~ "^/asset/{1,3}" { set $value "}"; }
}
server { listen 443 ssl; server_name other.example.com; location / { root /other; } }
'''


class Proxy:
    name = "1Panel-openresty-test"

    def __init__(self, tmp_path, mode="host", networks=None):
        self.path = tmp_path / "site.conf"
        self.path.write_text(HOST_CONFIG)
        self.mode = mode
        self.networks = networks or {}
        self.commands = []
        self.fail_test = False
        self.fail_reload = False

    def inspect(self, field):
        return {
            ".HostConfig.NetworkMode": self.mode,
            ".NetworkSettings.Networks": self.networks,
            ".Mounts": [{"Source": str(self.path.parent), "Destination": "/www/sites", "RW": True}],
        }[field]

    def nginx(self, option):
        self.commands.append(tuple(option))
        if option == ["-T"]:
            return f"# configuration file /www/sites/site.conf:\n{self.path.read_text()}\n"
        if option == ["-t"] and self.fail_test:
            self.fail_test = False
            raise installer.InstallError("test rejected updated config")
        if option == ["-s", "reload"] and self.fail_reload:
            self.fail_reload = False
            raise installer.InstallError("reload failed")
        return ""


@pytest.fixture
def docker_calls(monkeypatch):
    calls = []

    def command(*args):
        calls.append(args)
        if args[:3] == ("docker", "inspect", "--format"):
            assert args[-1] == "engineerip-asterisk"
            return json.dumps({"engineerip-telephony-internal": {"IPAddress": "172.22.0.4"}})
        return ""

    monkeypatch.setattr(installer, "command", command)
    monkeypatch.setattr(installer, "check_websocket", lambda *a, **kw: (True, "HTTP 101 with sip"))
    return calls


def test_only_matching_https_server_changes_and_second_install_is_idempotent():
    updated = installer.render_route(HOST_CONFIG, DOMAIN, "http://engineerip-asterisk:8088")
    assert updated.count("location = /ws") == 1
    assert "proxy_pass http://engineerip-asterisk:8088;" in updated
    assert 'proxy_set_header Connection "upgrade";' in updated
    before = installer.https_servers(HOST_CONFIG, DOMAIN)[0]
    after = installer.https_servers(updated, DOMAIN)[0]
    assert updated[:before.closing] == HOST_CONFIG[:before.closing]
    assert updated[after.end:] == HOST_CONFIG[before.end:]
    assert installer.render_route(updated, DOMAIN, "http://engineerip-asterisk:8088") == updated


def test_nginx_quoted_regex_comments_variables_and_literal_punctuation_are_not_blocks():
    text = '''map $host $value { default "{"; "}" ";"; }
# server { listen 443 ssl; server_name tel.engineerip.com; }
server { listen [::]:443 ssl; server_name "tel.engineerip.com";
    set $url http://${host}/path; location ~ "^/x/{1,2}" { set $v "\\\"}"; }
}'''
    assert len(installer.https_servers(text, DOMAIN)) == 1
    assert "location = /ws" in installer.render_route(text, DOMAIN, "http://127.0.0.1:8088")


@pytest.mark.parametrize("text", [
    "server { listen 80; server_name tel.engineerip.com; }",
    "server { listen 443 ssl; server_name *.engineerip.com; }",
    "server { listen 443 ssl; server_name tel.engineerip.com; }" * 2,
])
def test_ambiguous_or_non_https_server_is_not_edited(text):
    with pytest.raises(installer.InstallError):
        installer.render_route(text, DOMAIN, "http://127.0.0.1:8088")


def test_existing_unmanaged_exact_ws_route_is_not_overwritten():
    text = HOST_CONFIG.replace("ssl_certificate /certs/site.pem;", "location = /ws { proxy_pass http://existing:9000; }")
    with pytest.raises(installer.InstallError, match="unmanaged"):
        installer.render_route(text, DOMAIN, "http://127.0.0.1:8088")


def test_host_network_uses_host_loopback_without_network_attachment(tmp_path, docker_calls):
    proxy = Proxy(tmp_path)
    plan = installer.make_plan(proxy, DOMAIN)
    assert plan.upstream == "http://127.0.0.1:8088" and not plan.attach
    assert plan.direct_url == "ws://127.0.0.1:8088/ws"
    assert proxy.path.read_text() == HOST_CONFIG  # Planning is read-only.
    backup = installer.apply_plan(proxy, plan)
    assert backup.read_text() == HOST_CONFIG
    assert backup.stat().st_mode & 0o777 == 0o600
    assert proxy.path.read_text() == plan.updated
    assert ("-t",) in proxy.commands and ("-s", "reload") in proxy.commands
    assert not any("network" in c for c in docker_calls)


def test_bridge_network_never_uses_the_proxy_containers_loopback(tmp_path, docker_calls):
    proxy = Proxy(tmp_path, mode="bridge")
    plan = installer.make_plan(proxy, DOMAIN)
    assert plan.upstream == "http://engineerip-asterisk:8088"
    assert plan.direct_url == "ws://172.22.0.4:8088/ws" and plan.attach
    with pytest.raises(installer.InstallError, match="--connect-network"):
        installer.apply_plan(proxy, plan)
    assert proxy.path.read_text() == HOST_CONFIG
    assert not any("network" in c for c in docker_calls)
    installer.apply_plan(proxy, plan, connect_network=True)
    assert ("docker", "network", "connect", "engineerip-telephony-internal", proxy.name) in docker_calls
    assert not any("disconnect" in c for c in docker_calls)


def test_existing_shared_network_is_reused_without_adding_one(tmp_path, docker_calls):
    proxy = Proxy(tmp_path, mode="bridge", networks={"engineerip-telephony-internal": {}})
    assert not installer.make_plan(proxy, DOMAIN).attach


@pytest.mark.parametrize("failure", ["fail_test", "fail_reload"])
def test_rejection_restores_original_file_and_rolls_back_new_network(tmp_path, docker_calls, failure):
    proxy = Proxy(tmp_path, mode="bridge")
    plan = installer.make_plan(proxy, DOMAIN)
    setattr(proxy, failure, True)
    with pytest.raises(installer.InstallError, match="original vhost restored"):
        installer.apply_plan(proxy, plan, connect_network=True)
    assert proxy.path.read_text() == HOST_CONFIG
    assert ("docker", "network", "disconnect", "engineerip-telephony-internal", proxy.name) in docker_calls
    assert proxy.commands[-2:] == [("-t",), ("-s", "reload")]


def test_failed_asterisk_preflight_never_changes_file_network_or_reloads(tmp_path, docker_calls, monkeypatch):
    proxy = Proxy(tmp_path, mode="bridge")
    plan = installer.make_plan(proxy, DOMAIN)
    monkeypatch.setattr(installer, "check_websocket", lambda *a, **kw: (False, "HTTP 404"))
    with pytest.raises(installer.InstallError, match="Asterisk direct.*FAILED"):
        installer.apply_plan(proxy, plan, connect_network=True)
    assert proxy.path.read_text() == HOST_CONFIG
    assert proxy.commands == [("-T",)]
    assert not any("network" in c for c in docker_calls)


def test_concurrent_panel_edit_is_not_overwritten(tmp_path, docker_calls):
    proxy = Proxy(tmp_path, mode="bridge")
    plan = installer.make_plan(proxy, DOMAIN)
    proxy.path.write_text("# concurrent panel edit\n" + HOST_CONFIG)
    with pytest.raises(installer.InstallError, match="changed since inspection"):
        installer.apply_plan(proxy, plan, connect_network=True)
    assert proxy.path.read_text().startswith("# concurrent panel edit")
    assert not list(tmp_path.glob("*.bak"))
    assert any("disconnect" in c for c in docker_calls)


def test_single_file_bind_mount_keeps_the_inode(tmp_path, docker_calls):
    proxy = Proxy(tmp_path)
    proxy.inspect = lambda field: {
        ".HostConfig.NetworkMode": "host",
        ".Mounts": [{"Source": str(proxy.path), "Destination": "/www/sites/site.conf", "RW": True}],
    }[field]
    plan = installer.make_plan(proxy, DOMAIN)
    inode = proxy.path.stat().st_ino
    installer.apply_plan(proxy, plan)
    assert proxy.path.stat().st_ino == inode


def test_readonly_mount_or_unmounted_file_is_rejected(tmp_path):
    path = tmp_path / "site.conf"
    path.write_text(HOST_CONFIG)
    with pytest.raises(installer.InstallError, match="persistent"):
        installer.persistent_path("/www/sites/site.conf", [])
    with pytest.raises(installer.InstallError, match="read-only"):
        installer.persistent_path("/www/sites/site.conf", [{"Source": str(tmp_path), "Destination": "/www/sites", "RW": False}])


def test_no_success_claim_when_public_handshake_fails(tmp_path, docker_calls, monkeypatch, capsys):
    proxy = Proxy(tmp_path)
    monkeypatch.setattr(installer, "Proxy", lambda name: proxy)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    # Direct check passes, public check fails. Fast-forward the bounded retry clock.
    monkeypatch.setattr(installer, "check_websocket", lambda url, **kw: (not url.startswith("wss:"), "HTTP 404"))
    clock = iter([0, 20])
    monkeypatch.setattr(installer.time, "monotonic", lambda: next(clock))
    assert installer.main(["--domain", DOMAIN, "--proxy-container", proxy.name, "--apply"]) == 1
    assert "NOT VERIFIED" in capsys.readouterr().out
    assert proxy.path.read_text() != HOST_CONFIG  # Valid route retained, not a fake success.


def test_dry_run_makes_no_changes_and_does_not_probe_public_endpoint(tmp_path, docker_calls, monkeypatch):
    proxy = Proxy(tmp_path, mode="bridge")
    monkeypatch.setattr(installer, "Proxy", lambda name: proxy)
    monkeypatch.setattr(installer, "check_websocket", lambda *a, **kw: pytest.fail("dry-run must not connect"))
    assert installer.main(["--domain", DOMAIN, "--proxy-container", proxy.name]) == 0
    assert proxy.path.read_text() == HOST_CONFIG
    assert proxy.commands == [("-T",)]


def test_installer_with_real_nginx_changes_404_to_sip_upgrade_without_exposing_ari(tmp_path):
    """Real proxy/editor/handshake test; Docker discovery/networking are unit-tested.

    Local HTTP is intentional: public TLS is checked by the shipped checker on
    the VM, not simulated or claimed by this test.
    """
    import os
    import shutil
    import socket
    import subprocess
    import time
    import http.client
    from test_websocket_check import listener

    nginx = os.environ.get("NGINX_BIN") or shutil.which("nginx")
    if not nginx:
        pytest.skip("install nginx or set NGINX_BIN for the real-proxy test")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    (tmp_path / "empty").mkdir()
    site = tmp_path / "site.conf"
    template = f"server {{ listen 443; server_name {DOMAIN}; location / {{ root {tmp_path}/empty; }} }}\n"
    original = template.replace("listen 443;", f"listen 127.0.0.1:{port};")
    site.write_text(original)
    user = "user root;" if os.geteuid() == 0 else ""
    main = tmp_path / "nginx.conf"
    main.write_text(f'''daemon off;
master_process on;
worker_processes 1;
{user}
pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log notice;
events {{ worker_connections 256; }}
http {{
  access_log off;
  client_body_temp_path {tmp_path}/client_temp;
  proxy_temp_path {tmp_path}/proxy_temp;
  include {site};
}}
''')
    args = [nginx, "-p", str(tmp_path), "-c", str(main), "-e", str(tmp_path / "error.log")]

    class RealProxy:
        name = "local-real-nginx"

        def nginx(self, option):
            result = subprocess.run(args + option, capture_output=True, text=True, timeout=5)
            if result.returncode:
                raise installer.InstallError(result.stderr)
            return result.stdout

    proxy = RealProxy()
    proxy.nginx(["-t"])
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"ws://127.0.0.1:{port}/ws"

    def wait_for_upgrade(expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = installer.check_websocket(url, timeout=.5)
            if result[0] is expected and (expected or "HTTP 404" in result[1]):
                return
            time.sleep(.05)
        pytest.fail(f"Proxy check failed: {result}")

    try:
        wait_for_upgrade(False)
        with listener() as (upstream, requests):
            updated = installer.render_route(template, DOMAIN, upstream.replace("ws://", "http://").removesuffix("/ws"))
            updated = updated.replace("listen 443;", f"listen 127.0.0.1:{port};")
            plan = installer.Plan(site, original, updated, DOMAIN, "", False, upstream, "local test listener")
            backup = installer.apply_plan(proxy, plan)
            assert backup.read_text() == original
            wait_for_upgrade(True)
            assert requests[-1][0] == "/ws"
            assert requests[-1][1]["Sec-WebSocket-Protocol"] == "sip"
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                client.request("GET", "/ari/asterisk/info")
                assert client.getresponse().status == 404
            finally:
                client.close()
            assert all(path == "/ws" for path, _ in requests)
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
