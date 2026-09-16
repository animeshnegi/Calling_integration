import os

os.environ["FLASK_ENV"] = "testing"
os.environ["TELEPHONY_TOKEN"] = "test-token"
os.environ["ASTERISK_ARI_URL"] = "http://asterisk:8088/ari"
os.environ["ASTERISK_ARI_USER"] = "test-user"
os.environ["ASTERISK_ARI_PASSWORD"] = "test-password"
os.environ["ASTERISK_EXTENSIONS"] = "101,102"
os.environ["DEFAULT_EXTENSION"] = "101"

from app import create_app


class FakeAsterisk:
    def health(self):
        return {"system": "Asterisk Test"}

    def create_outbound_call(self, extension, phone, metadata=None):
        return "test-call-123"

    def hangup(self, channel_id):
        return None


def app_client():
    app = create_app()
    service = app.extensions["telephony_service"]
    service.asterisk = FakeAsterisk()
    return app.test_client()


def test_health_without_asterisk_dependency():
    client = app_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json["ok"] is True
    assert "extensions" not in response.json


def test_call_requires_auth():
    client = app_client()
    response = client.post("/api/v1/calls", json={"phone": "+16235551234", "extension": "101"})
    assert response.status_code == 401


def test_create_call_and_disposition():
    client = app_client()
    headers = {"Authorization": "Bearer test-token"}

    response = client.post(
        "/api/v1/calls",
        json={"phone": "+16235551234", "extension": "102", "contact_id": 582, "member_id": 37},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json["call"]["call_id"] == "test-call-123"
    assert response.json["call"]["extension"] == "102"

    response = client.post(
        "/api/v1/calls/test-call-123/disposition",
        json={"disposition": "follow_up", "notes": "Call Tuesday"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json["call"]["disposition"] == "follow_up"


def test_bad_extension():
    client = app_client()
    response = client.post(
        "/api/v1/calls",
        json={"phone": "+16235551234", "extension": "abc"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 400


def test_unconfigured_extension():
    client = app_client()
    response = client.post(
        "/api/v1/calls",
        json={"phone": "+16235551234", "extension": "103"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 400
    assert response.json["error"] == "extension is not configured"


def test_invalid_phone_is_rejected():
    client = app_client()
    response = client.post(
        "/api/v1/calls",
        json={"phone": "sip:attacker@example.com", "extension": "101"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 400


def test_browser_api_disabled_by_default():
    client = app_client()
    response = client.post(
        "/api/v1/browser/call",
        json={"phone": "+16235551234", "extension": "101"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 404


def test_extensions_are_authenticated():
    client = app_client()
    response = client.get("/api/v1/extensions")
    assert response.status_code == 401

    response = client.get("/api/v1/extensions", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.json["extensions"] == ["101", "102"]
