from pathlib import Path

from app import create_app
from app.config import Config
from app.models import Call


class FakeAsterisk:
    def health(self):
        return {"system": "Asterisk Test"}

    def create_outbound_call(self, call_id, extension, phone, provider_endpoint, metadata=None):
        return call_id

    def create_inbound_employee_leg(self, call_id, extension, customer_channel_id):
        return f"{call_id}-employee"

    def continue_in_dialplan(self, channel_id, context, extension):
        return None

    def hangup(self, channel_id):
        return None

    def hangup_call(self, employee_channel_id, customer_channel_id):
        return None


def app_client(tmp_path: Path):
    ready = tmp_path / "ari.ready"
    ready.touch()

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
        ARI_READY_PATH = str(ready)
        VOICEMAIL_PATH = str(tmp_path / "voicemail")
        VOICEMAIL_CONTEXT = "engineerip"
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
    store.save_number({
        "number": "+13025550101", "provider": "TestProvider", "description": "Extension 101",
        "inbound_extension": "101", "default_outbound": True,
    })
    store.save_number({
        "number": "+13025550102", "provider": "TestProvider", "description": "Extension 102",
        "inbound_extension": "102", "default_outbound": True,
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
    assert response.json["call"]["caller_id_number"] == "+13025550102"

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
    numbers = client.get("/api/v1/numbers?extension=101", headers={"Authorization": "Bearer test-token"})
    assert numbers.status_code == 200
    assert numbers.json["numbers"][0]["number"] == "+13025550101"
    assert numbers.json["numbers"][0]["default_outbound"] == 1


def test_webhook_crud_hides_token(tmp_path):
    client = app_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    response = client.post("/api/v1/webhooks", headers=headers, json={
        "name": "CRM", "url": "https://crm.example.com/events", "token": "secret",
        "events": "call.started,call.completed", "active": True,
    })
    assert response.status_code == 201
    webhook_id = response.json["webhook_id"]

    response = client.get("/api/v1/webhooks", headers=headers)
    assert response.status_code == 200
    assert response.json["webhooks"][0]["has_token"] is True
    assert "token" not in response.json["webhooks"][0]

    response = client.delete(f"/api/v1/webhooks/{webhook_id}", headers=headers)
    assert response.status_code == 200


def test_recording_list_requires_auth(tmp_path):
    client = app_client(tmp_path)
    assert client.get("/api/v1/recordings").status_code == 401
    response = client.get("/api/v1/recordings", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.json == {"recordings": []}


def test_admin_call_filters_and_assets(tmp_path):
    client = app_client(tmp_path)
    login = client.post("/admin/login", json={"username": "admin", "password": "test-admin-password-1234"})
    assert login.status_code == 200
    state = client.get("/admin/api/state")
    assert state.status_code == 200
    assert state.json["call_summary"]["total"] == 0
    assert client.get("/admin-assets/admin.css").status_code == 200
    assert client.get("/admin-assets/not-allowed.txt").status_code == 404

    service = client.application.extensions["telephony_service"]
    service.store.create(Call(
        call_id="ext-102-call", contact_id="contact-1", member_id=None,
        extension="102", phone="+16235550102", status="completed",
    ))
    response = client.get("/admin/api/calls?extension=102&limit=10")
    assert response.status_code == 200
    assert response.json["total"] == 1
    assert response.json["calls"][0]["call_id"] == "ext-102-call"


def test_voicemail_api_lists_and_streams_messages(tmp_path):
    client = app_client(tmp_path)
    settings = client.application.extensions["settings_store"]
    settings.save_extension({
        "extension": "101", "sip_username": "101", "sip_password": "secret101",
        "voicemail_enabled": True, "voicemail_pin": "1234",
    })
    directory = tmp_path / "voicemail" / "engineerip" / "101" / "INBOX"
    directory.mkdir(parents=True)
    (directory / "msg0000.txt").write_text("[message]\ncallerid=Test Caller\norigtime=1700000000\nduration=12\n")
    (directory / "msg0000.wav").write_bytes(b"RIFF-voicemail")
    headers = {"Authorization": "Bearer test-token"}

    mailboxes = client.get("/api/v1/voicemail/mailboxes", headers=headers)
    assert mailboxes.status_code == 200
    assert mailboxes.json["mailboxes"][0]["counts"]["new"] == 1

    response = client.get("/api/v1/voicemails?extension=101", headers=headers)
    assert response.status_code == 200
    assert response.json["total"] == 1
    assert response.json["voicemails"][0]["folder"] == "inbox"

    response = client.get("/api/v1/voicemails/101/inbox/msg0000/file", headers=headers)
    assert response.status_code == 200
    assert response.data == b"RIFF-voicemail"

    response = client.post("/api/v1/voicemails/101/inbox/msg0000/read", headers=headers)
    assert response.status_code == 200
    assert client.get("/api/v1/voicemails?extension=101&folder=old", headers=headers).json["total"] == 1


def test_recording_file_streams_from_private_ari(tmp_path):
    client = app_client(tmp_path)
    service = client.application.extensions["telephony_service"]
    service.store.create(Call(
        call_id="recorded-call", contact_id=None, member_id=None, extension="101", phone="+16235551234",
        recording_name="call-recorded-call", recording_format="wav", recording_status="finalized",
    ))

    class Upstream:
        status_code = 206
        headers = {"Content-Type": "audio/wav", "Content-Range": "bytes 0-3/4"}
        def iter_content(self, _size): return iter([b"RIFF"])
        def close(self): pass

    service.asterisk.open_stored_recording = lambda name, byte_range: Upstream()
    response = client.get(
        "/api/v1/recordings/recorded-call/file",
        headers={"Authorization": "Bearer test-token", "Range": "bytes=0-3"},
    )
    assert response.status_code == 206
    assert response.data == b"RIFF"
    assert response.content_type == "audio/wav"


def test_extension_user_is_scoped_and_cannot_change_system_settings(tmp_path):
    admin_client = app_client(tmp_path)
    app = admin_client.application
    settings = app.extensions["settings_store"]
    settings.save_user({
        "username": "agent101", "email": "agent@example.com", "extension": "101",
        "role": "user", "password": "long-agent-password", "active": True,
    })
    service = app.extensions["telephony_service"]
    service.store.create(Call(call_id="user-call", contact_id=None, member_id=None, extension="101", phone="+16235550101"))
    service.store.create(Call(call_id="other-call", contact_id=None, member_id=None, extension="102", phone="+16235550102"))

    client = app.test_client()
    assert client.post("/admin/login", json={"username": "agent101", "password": "long-agent-password"}).status_code == 200
    state = client.get("/admin/api/state")
    assert state.json["is_admin"] is False
    assert [row["extension"] for row in state.json["extensions"]] == ["101"]

    calls = client.get("/admin/api/calls?extension=102")
    assert calls.json["total"] == 1
    assert calls.json["calls"][0]["call_id"] == "user-call"

    started = client.post(
        "/admin/api/calls", json={"phone": "+919876543210", "extension": "102", "caller_id_number": "+13025550101"},
        headers={"X-CSRF-Token": state.json["csrf_token"]},
    )
    assert started.status_code == 201
    assert started.json["call"]["extension"] == "101"
    assert started.json["call"]["caller_id_number"] == "+13025550101"

    response = client.post(
        "/admin/api/settings", json={"recording_enabled": False},
        headers={"X-CSRF-Token": state.json["csrf_token"]},
    )
    assert response.status_code == 403


def test_scoped_crm_api_keys_are_hashed_and_enforced(tmp_path):
    client = app_client(tmp_path)
    store = client.application.extensions["settings_store"]
    _, token = store.create_api_key("CRM calls", "calls:read,config:read")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/extensions", headers=headers).status_code == 200
    assert client.get("/api/v1/calls", headers=headers).status_code == 200
    assert client.post("/api/v1/calls", headers=headers, json={"phone": "+919876543210", "extension": "101"}).status_code == 403
    assert client.get("/api/v1/webhooks", headers=headers).status_code == 403
    listed = store.list_api_keys()[0]
    assert listed["prefix"] == token[:12]
    assert "token" not in listed


def test_known_inbound_did_creates_crm_call_for_only_owner(tmp_path):
    client = app_client(tmp_path)
    service = client.application.extensions["telephony_service"]
    service.handle_ari_event({
        "type": "StasisStart", "args": ["inbound", "+13025550101", "101"],
        "channel": {"id": "carrier-channel-1", "state": "Up", "caller": {"number": "+919999999999"}},
    })
    inbound = next(call for call in service.store.all() if call.direction == "inbound")
    assert inbound.extension == "101"
    assert inbound.caller_id_number == "+13025550101"
    assert inbound.customer_channel_id == "carrier-channel-1"
    assert inbound.employee_channel_id.endswith("-employee")


def test_call_creation_idempotency_prevents_duplicate_originate(tmp_path):
    client = app_client(tmp_path)
    headers = {"Authorization": "Bearer test-token", "Idempotency-Key": "crm-request-12345"}
    first = client.post("/api/v1/calls", headers=headers, json={"phone": "+919876543210", "extension": "101"})
    second = client.post("/api/v1/calls", headers=headers, json={"phone": "+919876543210", "extension": "101"})
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json["idempotent_replay"] is True
    assert second.json["call"]["call_id"] == first.json["call"]["call_id"]
