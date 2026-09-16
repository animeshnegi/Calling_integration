from app.asterisk_client import AsteriskClient


class Response:
    ok = True
    content = b'{"id":"ok"}'

    def json(self):
        return {"id": "ok"}


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
