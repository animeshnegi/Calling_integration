from pathlib import Path

from app import create_app
from app.config import Config


class FakeAsterisk:
    def health(self):
        return {"system": "Asterisk Test"}

    def create_outbound_call(self, call_id, extension, phone, provider_endpoint, metadata=None):
        return call_id

    def hangup(self, channel_id):
        return None

    def hangup_call(self, employee_channel_id, customer_channel_id):
        return None


def app_client(tmp_path: Path):
    class TestingConfig(Config):
        FLASK_ENV = "testing"
        SECRET_KEY = "test-secret-key-which-is-long-enough"
        TELEPHONY_TOKEN = "test-token"
        ASTERISK_ARI_URL = "http://asterisk:8088/ari"
        ASTERISK_ARI_USER = "test-user"
        ASTERISK_ARI_PASSWORD = "test-password"
        ASTERISK_AMI_PASSWORD = "test-password"
        ASTERISK_EXTENSIONS = ("101", "102")
        DEFAULT_EXTENSION = "101"
        SETTINGS_DB_PATH = str(tmp_path / "settings.db")
        CALLS_DB_PATH = str(tmp_path / "calls.db")
        ASTERISK_DYNAMIC_CONFIG_PATH = str(tmp_path / "pjsip.dynamic.conf")
        ADMIN_USERNAME = "admin"
        ADMIN_PASSWORD = "test-admin-password-1234"

    app = create_app(TestingConfig)
    service = app.extensions["telephony_service"]
    store = app.extensions["settings_store"]
    store.save_extension({"extension": "101", "sip_username": "101", "sip_password": "secret101"})
    store.save_extension({"extension": "102", "sip_username": "102", "sip_password": "secret102"})
    store.save_provider({
        "name": "TestProvider", "server": "sip.example.com", "port": 5060,
        "username": "user", "password": "provider-password", "transport": "udp",
        "codecs": "ulaw,alaw", "allowed_ips": "198.51.100.10/32",
    })
    service.asterisk = FakeAsterisk()
    return app.test_client()


def test_health_without_asterisk_dependency(tmp_path):
    client = app_client(tmp_path)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json["ok"] is True


def test_call_requires_auth(tmp_path):
    client = app_client(tmp_path)
    response = client.post("/api/v1/calls", json={"phone": "+16235551234", "extension": "101"})
    assert response.status_code == 401


def test_create_call_and_disposition(tmp_path):
    client = app_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    response = client.post("/api/v1/calls", json={"phone": "+16235551234", "extension": "102", "contact_id": 582, "member_id": 37}, headers=headers)
    assert response.status_code == 201
    call_id = response.json["call"]["call_id"]
    assert response.json["call"]["extension"] == "102"

    response = client.post(f"/api/v1/calls/{call_id}/disposition", json={"disposition": "follow_up", "notes": "Call Tuesday"}, headers=headers)
    assert response.status_code == 200
    assert response.json["call"]["disposition"] == "follow_up"


def test_bad_extension(tmp_path):
    client = app_client(tmp_path)
    response = client.post("/api/v1/calls", json={"phone": "+16235551234", "extension": "abc"}, headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 400


def test_unconfigured_extension(tmp_path):
    client = app_client(tmp_path)
    response = client.post("/api/v1/calls", json={"phone": "+16235551234", "extension": "103"}, headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 400
    assert response.json["error"] == "extension is not configured"


def test_invalid_phone_is_rejected(tmp_path):
    client = app_client(tmp_path)
    response = client.post("/api/v1/calls", json={"phone": "sip:attacker@example.com", "extension": "101"}, headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 400


def test_browser_api_disabled_by_default(tmp_path):
    client = app_client(tmp_path)
    response = client.post("/api/v1/browser/call", json={"phone": "+16235551234", "extension": "101"}, headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 404


def test_extensions_are_authenticated(tmp_path):
    client = app_client(tmp_path)
    assert client.get("/api/v1/extensions").status_code == 401
    response = client.get("/api/v1/extensions", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.json["extensions"] == ["101", "102"]
