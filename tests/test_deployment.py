"""What the deployment files must keep promising for a browser call to work.

The browser softphone signs in over a WebSocket at wss://<service host>/ws.
That path has three links: the reverse proxy forwards it to Asterisk's
HTTP/WebSocket server, the compose file publishes that listener for the proxy,
and nothing may disable the two modules that serve it. When one link is missing
the softphone says "SIP WebSocket disconnected." and desk phones keep working,
so the failure is easy to misread as a SIP problem.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_compose_publishes_the_websocket_listener_for_the_reverse_proxy():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "127.0.0.1:8088:8088" in compose
    assert "127.0.0.1:8089:8089" in compose


def test_the_http_listener_serves_the_websocket_modules():
    entrypoint = (ROOT / "asterisk" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "bindport = 8088" in entrypoint
    assert "tlsbindaddr = 0.0.0.0:8089" in entrypoint

    modules = (ROOT / "asterisk" / "config" / "modules.conf").read_text(encoding="utf-8")
    assert "noload => res_http_websocket" not in modules
    assert "noload => res_pjsip_transport_websocket" not in modules


def test_the_documented_proxy_route_matches_the_one_the_softphone_dials():
    """The WebSocket rides the web domain, and the docs say so.

    The SIP address must resolve straight to the VM (or be the bare IP) so
    call media can travel - and an IP cannot hold a TLS certificate, so a
    wss:// URL built from the SIP address fails in every browser. The
    softphone dials the host serving its page; the /ws route therefore
    belongs on the web vhost, and the docs must show it there.
    """
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "proxy_pass http://127.0.0.1:8088;" in deployment
    assert "Upgrade" in deployment and "websocket" in deployment.lower()
    assert "wss://<the host this page is served from>/ws" in deployment

    phone = (ROOT / "web" / "phone.js").read_text(encoding="utf-8")
    admin = (ROOT / "web" / "admin.js").read_text(encoding="utf-8")
    # Never derive the WebSocket from the SIP domain or the Service address.
    assert 'location.host+"/ws"' in phone
    assert "credentials.server}/ws" not in admin
    assert "wss://${location.host}/ws" in admin


def test_the_docs_never_ask_for_a_manual_restart_after_the_service_address():
    """The configuration sync restarts Asterisk itself when transports change.

    `pjsip reload` never rebinds a transport, so `apply()` restarts Asterisk
    when the rendered transports change - and a doc that tells operators to
    restart the container by hand contradicts the code and invites a double
    restart that drops live calls for nothing.
    """
    for name in ("OPERATIONS.md", "DEPLOYMENT.md", "WEBRTC.md"):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        assert "restart the `asterisk` container after changing it" not in text, name
    assert "restarts Asterisk by itself" in (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")


def test_asterisk_is_started_by_absolute_path_so_core_restart_stays_in_place():
    """`core restart` re-executes argv[0] - that must not depend on PATH.

    With a bare `exec asterisk`, the re-exec after `core restart` can fail
    once the process has setuid'd to its runtime user; the process then exits
    0 and Docker's restart policy turns an ordinary in-place restart into a
    container restart - a RestartCount that climbs and every call dropped.
    """
    entrypoint = (ROOT / "asterisk" / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'ASTERISK_BIN="$(command -v asterisk)"' in entrypoint
    assert 'exec "$ASTERISK_BIN"' in entrypoint
    assert "exec asterisk " not in entrypoint

