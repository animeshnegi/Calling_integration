"""Exercise real HTTP Upgrade requests, not an ordinary GET /ws probe."""
import base64
import hashlib
import http.client
import importlib.util
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wscheck", ROOT / "scripts/check-softphone-websocket.py")
wscheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wscheck)


@contextmanager
def listener(status=101, *, headers=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            self.send_response(status)
            if status == 101:
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(hashlib.sha1((key + wscheck.GUID).encode()).digest()).decode()
                result = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Accept": accept, "Sec-WebSocket-Protocol": "sip"}
                result.update(headers or {})
                for name, value in result.items():
                    if value is not None:
                        self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"ws://127.0.0.1:{server.server_port}/ws", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_sip_handshake_sends_the_real_browser_upgrade_headers():
    with listener() as (url, seen):
        ok, message = wscheck.check_websocket(url, origin="https://tel.engineerip.com")
    assert ok, message
    path, headers = seen[0]
    assert path == "/ws"
    assert headers["Upgrade"] == "websocket"
    assert headers["Connection"] == "Upgrade"
    assert headers["Sec-WebSocket-Version"] == "13"
    assert headers["Sec-WebSocket-Protocol"] == "sip"
    assert headers["Origin"] == "https://tel.engineerip.com"
    assert len(base64.b64decode(headers["Sec-WebSocket-Key"])) == 16
    assert "Authorization" not in headers
    assert "registration and call media are separate" in message


@pytest.mark.parametrize("status", [200, 301, 302, 400, 401, 403, 404, 426, 500, 502, 503, 504])
def test_plain_http_or_redirect_never_counts_as_a_success(status):
    with listener(status) as (url, seen):
        ok, message = wscheck.check_websocket(url)
    assert not ok
    assert f"HTTP {status}" in message
    assert len(seen) == 1
    if status == 404:
        assert "404 alone does not identify which hop failed" in message


@pytest.mark.parametrize("headers,reason", [
    ({"Sec-WebSocket-Accept": "forged"}, "invalid Sec-WebSocket-Accept"),
    ({"Upgrade": None}, "without Upgrade"),
    ({"Connection": "keep-alive"}, "without Connection"),
    ({"Sec-WebSocket-Protocol": None}, "without the selected SIP subprotocol"),
    ({"Sec-WebSocket-Protocol": "other"}, "without the selected SIP subprotocol"),
])
def test_invalid_101_or_wrong_subprotocol_is_not_a_browser_compatible_upgrade(headers, reason):
    with listener(headers=headers) as (url, _):
        ok, message = wscheck.check_websocket(url)
    assert not ok and reason in message


def test_case_insensitive_upgrade_headers_and_connection_token_list():
    with listener(headers={"Upgrade": "WebSocket", "Connection": "keep-alive, Upgrade"}) as (url, _):
        assert wscheck.check_websocket(url)[0]


@pytest.mark.parametrize("url", ["https://example.com/ws", "wss:///ws", "wss://name:secret@example.com/ws", "wss://example.com/ws?token=secret", "wss://example.com/ws#part", "wss://example.com:bad/ws"])
def test_invalid_or_credential_bearing_urls_are_rejected_without_network_access(url, monkeypatch):
    monkeypatch.setattr(http.client, "HTTPSConnection", lambda *a, **kw: pytest.fail("must not connect"))
    assert not wscheck.check_websocket(url)[0]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected(timeout):
    assert not wscheck.check_websocket("wss://example.com/ws", timeout=timeout)[0]


def test_tls_validation_is_enabled_and_errors_are_not_silenced(monkeypatch):
    def failing_connection(host, port, **kwargs):
        assert host == "tel.engineerip.com" and port == 443
        assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["context"].check_hostname
        raise ssl.SSLCertVerificationError("untrusted certificate")

    monkeypatch.setattr(http.client, "HTTPSConnection", failing_connection)
    ok, message = wscheck.check_websocket("wss://tel.engineerip.com/ws")
    assert not ok and "SSLCertVerificationError" in message


def test_network_timeout_fails_cleanly(monkeypatch):
    monkeypatch.setattr(http.client, "HTTPConnection", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("timed out")))
    ok, message = wscheck.check_websocket("ws://127.0.0.1:8088/ws")
    assert not ok and "timed out" in message


def test_command_exit_code_only_passes_a_valid_upgrade(capsys):
    with listener() as (url, _):
        assert wscheck.main([url]) == 0
    assert capsys.readouterr().out.startswith("PASS:")
    with listener(404) as (url, _):
        assert wscheck.main([url]) == 1
    assert capsys.readouterr().out.startswith("FAIL:")


def test_nginx_snippet_exposes_only_the_exact_ws_handler():
    snippet = (ROOT / "deploy/nginx/softphone-websocket.conf").read_text()
    assert "location = /ws {" in snippet
    assert "proxy_pass http://127.0.0.1:8088;" in snippet
    assert "proxy_http_version 1.1;" in snippet
    assert "proxy_set_header Upgrade $http_upgrade;" in snippet
    assert 'proxy_set_header Connection "upgrade";' in snippet
    assert "proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;" in snippet
    assert "location / " not in snippet and "location /ari" not in snippet


def test_real_nginx_route_changes_404_to_valid_sip_upgrade_and_keeps_ari_private(tmp_path):
    """Optional local/CI integration: set NGINX_BIN or install nginx on PATH.

    No PBX, SIP account or outbound call is involved. An actual nginx process
    forwards the handshake to a deterministic local SIP-WebSocket test listener.
    """
    import os
    import shutil
    import signal
    import socket
    import subprocess
    import time

    nginx = os.environ.get("NGINX_BIN") or shutil.which("nginx")
    if not nginx:
        pytest.skip("install nginx or set NGINX_BIN for the real-proxy integration check")
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    proxy_url = f"ws://127.0.0.1:{port}/ws"
    (tmp_path / "empty").mkdir()
    config_path = tmp_path / "nginx.conf"
    snippet_path = tmp_path / "softphone.conf"
    config = f"""daemon off;
master_process on;
worker_processes 1;
pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log notice;
events {{ worker_connections 256; }}
http {{
    access_log off;
    client_body_temp_path {tmp_path}/client_temp;
    proxy_temp_path {tmp_path}/proxy_temp;
    server {{
        listen 127.0.0.1:{port};
        location / {{ root {tmp_path}/empty; }}
        SNIPPET
    }}
}}
"""

    def wait_for_result(expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = wscheck.check_websocket(proxy_url, timeout=.5)
            if result[0] is expected and (expected or "HTTP 404" in result[1]):
                return result
            time.sleep(.05)
        pytest.fail(f"nginx did not become ready: {result}; {(tmp_path / 'error.log').read_text()}")

    with listener() as (upstream_url, seen):
        # Only the ephemeral upstream port changes; all shipped directives are tested.
        snippet = (ROOT / "deploy/nginx/softphone-websocket.conf").read_text()
        snippet_path.write_text(snippet.replace("http://127.0.0.1:8088", upstream_url.replace("ws://", "http://").removesuffix("/ws")))
        config_path.write_text(config.replace("SNIPPET", ""))
        args = [nginx, "-p", str(tmp_path), "-c", str(config_path), "-e", str(tmp_path / "error.log")]
        validation = subprocess.run(args + ["-t"], capture_output=True, text=True, timeout=5)
        assert validation.returncode == 0, validation.stderr
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            assert not wait_for_result(False)[0]  # Reproduce the missing /ws route.
            assert seen == []                   # It never reached the SIP listener.
            config_path.write_text(config.replace("SNIPPET", f"include {snippet_path};"))
            validation = subprocess.run(args + ["-t"], capture_output=True, text=True, timeout=5)
            assert validation.returncode == 0, validation.stderr
            process.send_signal(signal.SIGHUP)
            assert wait_for_result(True)[0]      # Same URL now upgrades with protocol sip.
            assert seen[-1][0] == "/ws"         # No trailing slash or URI rewriting.
            assert seen[-1][1]["Sec-WebSocket-Protocol"] == "sip"
            assert seen[-1][1]["Upgrade"].lower() == "websocket"
            assert seen[-1][1]["Connection"].lower() == "upgrade"
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                client.request("GET", "/ari/asterisk/info")
                assert client.getresponse().status == 404
            finally:
                client.close()
            assert all(path == "/ws" for path, _ in seen)
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
