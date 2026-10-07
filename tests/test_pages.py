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


def test_static_assets_keep_their_cache_policy_and_api_answers_are_not_cached(tmp_path: Path):
    client = page_client(tmp_path)
    for path in ("/phone.css", "/phone.js", "/manifest.json"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "public, max-age=3600", path
    for path in ("/phone", "/api/v1/extensions"):
        assert client.get(path).headers["Cache-Control"] == "no-store", path
