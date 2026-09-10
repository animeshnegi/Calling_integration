import os

os.environ["TELEPHONY_TOKEN"] = "test-token"
os.environ["ASTERISK_ARI_URL"] = "http://asterisk:8088/ari"

from app import create_app


class FakeAsterisk:
    def health(self):
        return {"system": "Asterisk Test"}

    def create_outbound_call(self, extension, phone, metadata=None):
        return "test-call-123"

    def hangup(self, channel_id):
        return None


def test_health_without_asterisk_dependency(monkeypatch):
    app = create_app()
    service = app.extensions["telephony_service"]
    service.asterisk = FakeAsterisk()
    client = app.test_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json["ok"] is True


def test_call_requires_auth():
    app = create_app()
    client = app.test_client()
    response = client.post("/api/v1/calls", json={"phone": "+16235551234", "extension": "101"})
    assert response.status_code == 401


def test_create_call_and_disposition(monkeypatch):
    app = create_app()
    service = app.extensions["telephony_service"]
    service.asterisk = FakeAsterisk()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    response = client.post(
        "/api/v1/calls",
        json={"phone": "+16235551234", "extension": "101", "contact_id": 582, "member_id": 37},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json["call"]["call_id"] == "test-call-123"

    response = client.post(
        "/api/v1/calls/test-call-123/disposition",
        json={"disposition": "follow_up", "notes": "Call Tuesday"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json["call"]["disposition"] == "follow_up"


def test_bad_extension():
    app = create_app()
    service = app.extensions["telephony_service"]
    service.asterisk = FakeAsterisk()
    client = app.test_client()
    response = client.post(
        "/api/v1/calls",
        json={"phone": "+16235551234", "extension": "abc"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 400
