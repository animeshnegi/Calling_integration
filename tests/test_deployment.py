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
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "proxy_pass http://127.0.0.1:8088;" in deployment
    assert "Upgrade" in deployment and "websocket" in deployment.lower()
