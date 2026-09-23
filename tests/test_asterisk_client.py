from app.asterisk_client import AsteriskClient


class Response:
    ok = True
    content = b'{"id":"ok"}'

    def json(self):
        return {"id": "ok"}


def test_stored_recording_file_uses_private_ari_and_range(monkeypatch):
    captured = {}

    class StreamResponse:
        status_code = 206
        headers = {"Content-Type": "audio/wav"}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return StreamResponse()

    monkeypatch.setattr("app.asterisk_client.requests.get", fake_get)
    client = AsteriskClient(type("Config", (), {
        "ASTERISK_ARI_URL": "http://asterisk:8088/ari",
        "ASTERISK_ARI_USER": "u", "ASTERISK_ARI_PASSWORD": "p", "ASTERISK_ARI_APP": "app",
    }))
    response = client.open_stored_recording("call-123", "bytes=10-")
    assert response.status_code == 206
    assert captured["url"].endswith("/recordings/stored/call-123/file")
    assert captured["headers"] == {"Range": "bytes=10-"}
    assert captured["auth"] == ("u", "p")


def test_originate_sends_variables_as_json_body(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.asterisk_client.requests.request", fake_request)
    client = AsteriskClient(type("Config", (), {
        "ASTERISK_ARI_URL": "http://asterisk:8088/ari",
        "ASTERISK_ARI_USER": "u",
        "ASTERISK_ARI_PASSWORD": "p",
        "ASTERISK_ARI_APP": "engineerip",
    }))
    client.create_outbound_call("call-1", "101", "+13025551234", "provider-1", {"contact_id": "42"})

    assert captured["json"]["variables"]["EIP_CALL_ID"] == "call-1"
    assert captured["json"]["variables"]["EIP_CONTACT_ID"] == "42"
    assert "variables" not in captured["params"]


def test_customer_leg_uses_assigned_callback_number(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.asterisk_client.requests.request", fake_request)
    client = AsteriskClient(type("Config", (), {
        "ASTERISK_ARI_URL": "http://asterisk:8088/ari", "ASTERISK_ARI_USER": "u",
        "ASTERISK_ARI_PASSWORD": "p", "ASTERISK_ARI_APP": "engineerip",
    }))
    client.create_customer_leg("call-1", "+919999999999", "provider-1", "employee-1", "+13025550101")
    assert captured["params"]["callerId"] == "+13025550101"
