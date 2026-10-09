"""The API, webhook and landing pages say what the running code does.

Each check reads the page or the source and compares it with the code, so a
documented endpoint, event, header or example that drifts from the server fails
here instead of in a customer's integration.
"""
import hashlib
import hmac
import html
import json
import re
from pathlib import Path

import pytest

from app import admin as admin_module
from app.admin import SettingsStore
from app.services import TelephonyService
from tests.test_provisioning import customer_client, make_app

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DOC = (WEB / "documentation.html").read_text(encoding="utf-8")
LANDING = (WEB / "index.html").read_text(encoding="utf-8")
PICKER_SOURCE = (WEB / "admin.js").read_text(encoding="utf-8")
LOGO = "https://engineerip.com/static/img/logo.png"

PILL_RE = re.compile(r'<span class="pill (?:get|post|del)">(GET|POST|DELETE)</span>\s*<code>(/api/v1/[^<]+)</code>')
# Routes that are not part of the public API: a private engine hook, off by default.
INTERNAL_ROUTES = {"/api/v1/webhooks/ari"}


@pytest.fixture(autouse=True)
def fresh_login_budget():
    admin_module._LOGIN_BUCKETS.clear()
    yield
    admin_module._LOGIN_BUCKETS.clear()


def normalise(path: str) -> str:
    """`/api/v1/calls/<call_id>` and `/api/v1/calls/<int:id>` compare equal to `<>`."""
    return re.sub(r"<[^>]+>", "<>", html.unescape(path).split("?", 1)[0])


def documented_endpoints():
    return {(method, normalise(path)) for method, path in PILL_RE.findall(DOC)}


def test_every_documented_endpoint_exists_with_that_method(tmp_path):
    app = make_app(tmp_path)
    served = {(method, normalise(rule.rule)) for rule in app.url_map.iter_rules() for method in rule.methods}
    missing = sorted(endpoint for endpoint in documented_endpoints() if endpoint not in served)
    assert not missing, f"documented but not served: {missing}"


def test_every_public_api_route_is_documented(tmp_path):
    app = make_app(tmp_path)
    documented = documented_endpoints()
    undocumented = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/v1/") or rule.rule in INTERNAL_ROUTES:
            continue
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            if (method, normalise(rule.rule)) not in documented:
                undocumented.append((method, rule.rule))
    assert not undocumented, f"served but not documented: {sorted(undocumented)}"


def _emitted_events() -> set[str]:
    names: set[str] = set()
    for source in ("app/services.py", "app/routes.py"):
        text = (ROOT / source).read_text(encoding="utf-8")
        names |= set(re.findall(r'notify_crm\(\s*"(call\.[a-z_]+)"', text))
        if "notify_crm(f\"call.recording_{'finished'" in text:
            # The recording lifecycle is one f-string that yields finished or failed.
            names |= {"call.recording_finished", "call.recording_failed"}
    return names


def test_documented_events_are_exactly_the_events_the_server_sends():
    section = DOC[DOC.index('<section id="events">'):DOC.index('<section id="devices">')]
    documented = set(re.findall(r"call\.[a-z_]+", section))
    emitted = _emitted_events()
    assert documented == emitted, {
        "documented_only": sorted(documented - emitted), "missing_from_docs": sorted(emitted - documented),
    }


def test_console_event_picker_offers_every_event_the_server_sends():
    index = PICKER_SOURCE.index('name="events"')
    listed = set(re.findall(r"'([^']+)'", re.search(r"\[([^\]]*)\]", PICKER_SOURCE[index:]).group(1)))
    assert listed == _emitted_events() | {"*"}, {
        "not_sent": sorted(listed - _emitted_events() - {"*"}),
        "cannot_subscribe": sorted(_emitted_events() - listed),
    }


def test_webhook_signature_matches_the_documented_verification(monkeypatch):
    captured = {}

    class Response:
        ok = True
        status_code = 200

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.services.requests.post", fake_post)
    payload = {"event": "call.completed", "call": {"call_id": "abc-123", "status": "completed"}, "reason": "hangup_requested"}
    TelephonyService._send_webhook("https://crm.example/events", "shared-secret", payload)

    body, headers = captured["data"], captured["headers"]
    assert headers["Authorization"] == "Bearer shared-secret"
    # The documented check: HMAC-SHA256 over `timestamp + "." + raw body`, sent as `sha256=<hex>`.
    expected = hmac.new(b"shared-secret", headers["X-EngineerIP-Timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["X-EngineerIP-Signature"] == f"sha256={expected}"
    assert json.loads(body) == payload
    # The page's sample body names the same top-level keys the server sends.
    assert '{"event":"call.completed","call":{' in DOC
    assert '"data"' not in DOC.split('<section id="webhooks">')[1].split('<section id="events">')[0]


def test_documented_sip_username_is_the_shape_the_server_generates():
    generated = SettingsStore.generate_sip_username("101", "+13025550001", "Meridian")
    assert generated == "MERIDIAN_101_13025550001"
    assert "MERIDIAN_101_13025550001" in DOC
    assert "KUDGTE_101" not in DOC


def test_the_documentation_states_the_three_digit_rule():
    assert "A three-digit extension is resolved only within the current phone number." in DOC


def test_the_documented_curl_example_is_a_valid_shell_command():
    # A doubled backslash in the page would make the continuation line a literal, breaking the command.
    assert "\\\\\n" not in DOC


def test_the_documentation_page_is_public_and_names_this_deployment(tmp_path):
    app = make_app(tmp_path)
    anonymous = app.test_client().get("/documentation")
    assert anonymous.status_code == 200
    assert b"{{API_BASE}}" not in anonymous.data and b"{{SIP_HOST}}" not in anonymous.data


def test_landing_page_nav_links_softphone_and_api_docs(tmp_path):
    app = make_app(tmp_path)
    page = app.test_client().get("/").data.decode()
    nav = re.search(r'<nav class="wrap">(.*?)</nav>', page, re.S).group(1)
    assert 'href="/phone"' in nav and "Softphone" in nav
    assert 'href="/documentation"' in nav and "API" in nav
    # The softphone is explained on the landing page, not only linked.
    section = page[page.index('id="softphone"'):page.index("</section>", page.index('id="softphone"'))]
    assert 'href="/phone"' in section
    for step in ("Sign in", "Dial", "In a call"):
        assert step in section, step


@pytest.mark.parametrize("name", ["index.html", "documentation.html", "phone.html", "admin-login.html", "admin.html"])
def test_every_page_has_the_brand_favicon(name):
    assert f'rel="icon" type="image/png" href="{LOGO}"' in (WEB / name).read_text(encoding="utf-8"), name
