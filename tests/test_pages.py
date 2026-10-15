"""The pages a browser is served: which files exist, what they declare, and
what a bare probe such as /favicon.ico is answered with."""
from pathlib import Path

import pytest

from app import create_app
from app.config import Config

WEB = Path(__file__).resolve().parent.parent / "web"
LOGO = "https://engineerip.com/static/img/logo.png"
PAGES = sorted(WEB.glob("*.html"))


def page_client(tmp_path: Path, config_class=Config):
    class TestingConfig(config_class):
        FLASK_ENV = "testing"
        SECRET_KEY = "test-secret-key-which-is-long-enough"
        ASTERISK_EXTENSIONS = ("101",)
        DEFAULT_EXTENSION = "101"
        SETTINGS_DB_PATH = str(tmp_path / "settings.db")
        CALLS_DB_PATH = str(tmp_path / "calls.db")
        VOICEMAIL_PATH = str(tmp_path / "voicemail")
        ASTERISK_DYNAMIC_CONFIG_PATH = str(tmp_path / "pjsip.dynamic.conf")
        ADMIN_USERNAME = "admin"
        ADMIN_PASSWORD = "test-admin-password-1234"

    return create_app(TestingConfig).test_client()


@pytest.mark.parametrize("page", PAGES, ids=lambda page: page.name)
def test_every_page_declares_the_engineerip_logo_as_its_favicon(page: Path):
    html = page.read_text(encoding="utf-8")
    assert f'<link rel="icon" type="image/png" href="{LOGO}">' in html
    assert f'<link rel="apple-touch-icon" href="{LOGO}">' in html


@pytest.mark.parametrize("page", PAGES, ids=lambda page: page.name)
def test_no_page_points_at_a_favicon_the_app_does_not_serve(page: Path):
    # The documentation page used to ask for /favicon.ico, which answered 404.
    assert "/favicon.ico" not in page.read_text(encoding="utf-8")


def test_a_bare_favicon_probe_is_answered_with_the_logo(tmp_path: Path):
    response = page_client(tmp_path).get("/favicon.ico")
    assert response.status_code == 302
    assert response.headers["Location"] == LOGO
    assert "public" in response.headers["Cache-Control"]


def test_the_favicon_can_be_pointed_at_another_image(tmp_path: Path):
    class BrandedConfig(Config):
        FAVICON_URL = "https://cdn.example.com/brand.png"

    response = page_client(tmp_path, BrandedConfig).get("/favicon.ico")
    assert response.headers["Location"] == "https://cdn.example.com/brand.png"


def test_the_softphone_surface_is_served(tmp_path: Path):
    client = page_client(tmp_path)
    phone = client.get("/phone")
    assert phone.status_code == 200
    assert "EngineerIP Phone" in phone.get_data(as_text=True)
    assert "/phone.js" in phone.get_data(as_text=True)

    manifest = client.get("/manifest.json")
    assert manifest.status_code == 200
    payload = manifest.get_json()
    assert payload["start_url"] == "/phone"
    assert LOGO in {icon["src"] for icon in payload["icons"]}

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert worker.headers["Service-Worker-Allowed"] == "/"


def test_the_softphone_loads_its_sip_stack_from_this_origin(tmp_path: Path):
    """The SIP stack is vendored, not fetched from a CDN.

    The page used to load JsSIP from cdn.jsdelivr.net/npm/jssip@3.10.1, a path
    the npm package does not actually ship - the request came back as a 404
    page, so every sign-in failed with "SIP library is not loaded.". The stack
    now ships with the app; only a pinned upstream build may stand in as a
    fallback.
    """
    client = page_client(tmp_path)
    html = client.get("/phone").get_data(as_text=True)
    assert '<script src="/jssip.min.js">' in html
    assert "cdn.jsdelivr.net/npm/jssip" not in html
    assert "JsSIP@3.10.1" in html  # the pinned fallback, if any, stays pinned

    stack = client.get("/jssip.min.js")
    assert stack.status_code == 200
    body = stack.get_data(as_text=True)
    assert "JsSIP v3.10.1" in body
    assert ".JsSIP=" in body  # the browser build defines window.JsSIP


def test_scripts_are_served_as_javascript_so_nosniff_browsers_run_them(tmp_path: Path):
    """Every response carries X-Content-Type-Options: nosniff - which is good -
    but it means a script served with an unknown or wrong type is a script the
    browser refuses to execute, and the phone then says "SIP library is not
    loaded" even though the file arrived."""
    client = page_client(tmp_path)
    for path in ("/phone.js", "/jssip.min.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["X-Content-Type-Options"] == "nosniff", path
        assert response.mimetype in ("text/javascript", "application/javascript"), (path, response.content_type)
    css = client.get("/phone.css")
    assert css.mimetype == "text/css"


def test_static_assets_keep_their_cache_policy_and_api_answers_are_not_cached(tmp_path: Path):
    client = page_client(tmp_path)
    for path in ("/phone.css", "/phone.js", "/jssip.min.js", "/manifest.json"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "public, max-age=3600", path
    for path in ("/phone", "/api/v1/extensions"):
        assert client.get(path).headers["Cache-Control"] == "no-store", path

def test_the_softphone_button_only_shows_for_a_browser_extension():
    """The console's Softphone button is rendered only inside a WebRTC check.

    Without the extension's Browser phone option there is no browser identity
    to sign in with, so the button must not appear for a user or an
    administrator - a visible button that can only fail is worse than none.
    Both consoles share this renderer, so one gate covers both roles.
    """
    admin_js = (Path(__file__).resolve().parents[1] / "web" / "admin.js").read_text()
    assert '${x.webrtc_enabled ? `<button class="btn primary sm" data-softphone=' in admin_js
    assert '${c.webrtc_enabled ? `<button class="btn primary sm" type="button" data-softphone=' in admin_js
    for line in admin_js.splitlines():
        if "<button" in line and "data-softphone=" in line:
            assert "webrtc_enabled ?" in line, f"ungated softphone button: {line.strip()}"
    # The window that opens signs in on the browser identity, never the plain
    # one a desk phone or Zoiper uses.
    assert "sip_username: credentials.webrtc_sip_username || credentials.sip_username" in admin_js

