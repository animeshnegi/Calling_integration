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
    assert login.is_json and login.json["ok"] is True
    repeated = client.post("/admin/login", json={"username": "admin", "password": "test-admin-password-1234"})
    assert repeated.status_code == 200
    assert repeated.is_json and repeated.json["already_authenticated"] is True
    assert repeated.location is None
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
    user_id = settings.save_user({
        "username": "agent101", "email": "agent@example.com",
        "role": "user", "password": "long-agent-password", "active": True,
    })
    settings.save_extension({
        "extension": "101", "sip_username": "101", "active": True,
        "recording_enabled": False,
    }, owner_user_id=user_id)
    service = app.extensions["telephony_service"]
    service.store.create(Call(call_id="user-call", contact_id=None, member_id=None, extension="101", phone="+16235550101"))
    service.store.create(Call(call_id="other-call", contact_id=None, member_id=None, extension="102", phone="+16235550102"))

    client = app.test_client()
    assert client.post("/admin/login", json={"username": "agent101", "password": "long-agent-password"}).status_code == 200
    state = client.get("/admin/api/state")
    assert state.json["is_admin"] is False
    assert [row["extension"] for row in state.json["extensions"]] == ["101"]

    calls = client.get("/admin/api/calls?extension=102")
    assert calls.status_code == 404
    calls = client.get("/admin/api/calls")
    assert calls.json["total"] == 1
    assert calls.json["calls"][0]["call_id"] == "user-call"

    started = client.post(
        "/admin/api/calls", json={"phone": "+919876543210", "extension": "101", "caller_id_number": "+13025550101"},
        headers={"X-CSRF-Token": state.json["csrf_token"]},
    )
    assert started.status_code == 201
    assert started.json["call"]["extension"] == "101"
    assert started.json["call"]["caller_id_number"] == "+13025550101"

    preference = client.post(
        "/admin/api/profile/recording", json={"enabled": True},
        headers={"X-CSRF-Token": state.json["csrf_token"]},
    )
    assert preference.status_code == 200
    assert next(row for row in settings.list_extensions() if row["extension"] == "101")["recording_enabled"] == 1
    # The global switch remains off, so opting in does not start recording yet.
    assert settings.get_settings()["recording_enabled"] == "false"

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


def test_public_signup_and_customer_resources_are_tenant_isolated(tmp_path):
    client = app_client(tmp_path)
    created = client.post("/signup", json={
        "full_name": "Customer One", "company_name": "One Company", "job_role": "Owner", "phone": "+13025550199",
        "username": "customer-one", "email": "one@example.com", "password": "a-secure-customer-password",
    })
    assert created.status_code == 201
    store = client.application.extensions["settings_store"]
    user = next(row for row in store.list_users() if row["username"] == "customer-one")
    assert user["role"] == "user" and user["extension"] == ""

    other_id = store.save_user({
        "username": "customer-two", "email": "two@example.com", "password": "another-secure-password", "role": "user",
    })
    store.save_extension({"extension": "201", "sip_password": "customer-one-sip", "active": True}, user["id"])
    store.save_extension({"extension": "202", "sip_password": "customer-two-sip", "active": True}, other_id)
    store.save_provider({
        "name": "IPComms", "server": "sip.example.com", "username": "platform",
        "password": "provider-secret", "allowed_ips": "203.0.113.10/32", "active": True,
    })
    store.save_number({
        "number": "+13025550201", "provider": "IPComms", "inbound_extension": "201",
        "owner_user_id": user["id"], "monthly_price": "5.00", "billing_cycle_day": 12, "active": True,
    })
    store.save_number({
        "number": "+13025550202", "provider": "IPComms", "inbound_extension": "202",
        "owner_user_id": other_id, "monthly_price": "7.00", "billing_cycle_day": 15, "active": True,
    })

    customer = client.application.test_client()
    assert customer.post("/admin/login", json={"username": "customer-one", "password": "a-secure-customer-password"}).status_code == 200
    state = customer.get("/admin/api/state").json
    assert [row["extension"] for row in state["extensions"]] == ["201"]
    assert [row["number"] for row in state["phone_numbers"]] == ["+13025550201"]
    assert state["providers"] == []
    assert len(state["invoices"]) == 1
    assert state["invoices"][0]["amount_cents"] == 500

    key = customer.post("/admin/api/api-keys", json={"name": "Customer CRM", "scopes": "config:read,calls:read"}, headers={"X-CSRF-Token": state["csrf_token"]})
    assert key.status_code == 201 and key.json["token"].startswith("eip_")
    api_headers = {"Authorization": f"Bearer {key.json['token']}"}
    assert customer.get("/api/v1/extensions", headers=api_headers).json["extensions"] == ["201"]
    assert [row["number"] for row in customer.get("/api/v1/numbers", headers=api_headers).json["numbers"]] == ["+13025550201"]
    assert customer.get("/api/v1/providers", headers=api_headers).json["providers"] == []
    service = client.application.extensions["telephony_service"]
    service.store.create(Call(call_id="tenant-own", contact_id=None, member_id=None, extension="201", phone="+13025550001"))
    service.store.create(Call(call_id="tenant-other", contact_id=None, member_id=None, extension="202", phone="+13025550002"))
    assert customer.get("/api/v1/calls/tenant-own", headers=api_headers).status_code == 200
    assert customer.get("/api/v1/calls/tenant-other", headers=api_headers).status_code == 404
    cancellation = customer.post(
        "/admin/api/numbers/+13025550201/discontinue", headers={"X-CSRF-Token": state["csrf_token"]},
    )
    assert cancellation.status_code == 200 and cancellation.json["discontinue_at"]
    other_cancellation = customer.post(
        "/admin/api/numbers/+13025550202/discontinue", headers={"X-CSRF-Token": state["csrf_token"]},
    )
    assert other_cancellation.status_code == 400

    request_result = customer.post(
        "/admin/api/requests", json={"request_type": "number", "details": "New York area code"},
        headers={"X-CSRF-Token": state["csrf_token"]},
    )
    assert request_result.status_code == 201
    duplicate_request = customer.post(
        "/admin/api/requests", json={"request_type": "number", "details": "A second pending request"},
        headers={"X-CSRF-Token": state["csrf_token"]},
    )
    assert duplicate_request.status_code == 400
    assert "pending number request" in duplicate_request.json["error"]
    admin = client.application.test_client()
    assert admin.post("/admin/login", json={"username": "admin", "password": "test-admin-password-1234"}).status_code == 200
    admin_state = admin.get("/admin/api/state").json
    assert admin_state["pending_request_count"] == 1
    sip = admin.post("/admin/api/sip-accounts", json={
        "owner_user_id": user["id"], "label": "Primary device", "sip_username": "customer-one-device",
        "sip_password": "strong-sip-password", "server": "sip.eip.example", "phone_number": "+13025550201", "extension": "201",
    }, headers={"X-CSRF-Token": admin_state["csrf_token"]})
    assert sip.status_code == 200
    refreshed = customer.get("/admin/api/state").json
    account_id = refreshed["sip_accounts"][0]["id"]
    credentials = customer.get(f"/admin/api/sip-accounts/{account_id}/credentials")
    assert credentials.status_code == 200
    assert credentials.json["sip_account"]["sip_password"] == "strong-sip-password"
    route = customer.post("/admin/api/call-routes", json={
        "phone_number": "+13025550201", "name": "Main", "route": {"nodes": [
            {"type": "business_hours", "start": "09:00", "end": "17:00", "days": [1, 2, 3, 4, 5]},
            {"type": "simultaneous", "extensions": ["201"], "timeout": 25},
            {"type": "extension", "extension": "201"}, {"type": "voicemail", "mailbox": "201"}
        ]}, "active": True,
    }, headers={"X-CSRF-Token": state["csrf_token"]})
    assert route.status_code == 200
    assert customer.get("/admin/api/state").json["call_routes"][0]["route"]["nodes"][1]["type"] == "simultaneous"
    assert admin.get("/admin/api/state").json["call_routes"][0]["owner_user_id"] == user["id"]
    analytics = customer.get("/admin/api/analytics")
    assert analytics.status_code == 200
    assert {"answer_rate", "average_duration_seconds", "daily", "extensions"} <= set(analytics.json)
    exported = customer.get("/admin/api/calls/export.csv")
    assert exported.status_code == 200 and exported.mimetype == "text/csv"
    assert "Call ID,Started,Direction" in exported.get_data(as_text=True)

    key_id = customer.get("/admin/api/state").json["api_keys"][0]["id"]
    assert customer.delete(f"/admin/api/api-keys/{key_id}", headers={"X-CSRF-Token": state["csrf_token"]}).status_code == 200
    assert store.authenticate_api_key(key.json["token"]) is None


def test_secure_customer_provisioning_upserts_without_changing_role(tmp_path):
    from app.admin import SettingsStore
    from app.manage_customer import provision_customer

    store = SettingsStore(str(tmp_path / "customers.db"), "provisioning-secret-key")
    user_id, created = provision_customer(store, "EngineerIP", "owner@engineerip.example", "first-secure-password")
    assert created is True
    user = store.get_user(user_id)
    assert user["username"] == "engineerip" and user["role"] == "user" and user["extension"] == ""
    same_id, created = provision_customer(store, "engineerip", "billing@engineerip.example", "second-secure-password")
    assert same_id == user_id and created is False
    assert store.authenticate("engineerip", "second-secure-password")["email"] == "billing@engineerip.example"


class FakeEndpointList:
    """ARI endpoint inventory; proves live registration is overlaid on stored state."""

    def __init__(self, rows):
        self.rows = rows

    def list_endpoints(self):
        return self.rows


def test_device_status_overlays_live_state_and_scopes_to_the_owner(tmp_path):
    """Regression: /admin/api/device-status used to raise NameError (HTTP 500).

    It resolved the caller through helpers that do not exist in app.admin, so
    the live-registration badge was dead in both consoles.
    """
    client = app_client(tmp_path)
    assert client.post("/admin/login", json={"username": "admin", "password": "test-admin-password-1234"}).status_code == 200
    store = client.application.extensions["settings_store"]
    service = client.application.extensions["telephony_service"]

    def provision(username: str, extension: str, number: str, sip_username: str = "", linked: bool = True) -> tuple[int, int]:
        owner_id = store.save_user({
            "username": username, "email": f"{username}@example.com",
            "password": f"{username}-secure-password", "role": "user",
        })
        store.save_extension({"extension": extension, "sip_password": f"{extension}-sip", "active": True}, owner_id)
        store.save_number({
            "number": number, "provider": "TestProvider", "inbound_extension": extension,
            "owner_user_id": owner_id, "active": True,
        })
        account_id = store.save_sip_account({
            "label": f"Desk phone {extension}", "sip_username": sip_username, "sip_password": f"{extension}-handset",
            "server": "sip.example.com", "phone_number": number,
            "extension": extension if linked else "", "active": True,
        }, owner_id)
        return owner_id, account_id

    # An account linked to an extension authenticates as that extension; a device
    # account with no extension keeps its own name.
    _, account_one = provision("device-one", "301", "+13025550301")
    _, account_two = provision("device-two", "302", "+13025550302", "device302", linked=False)

    # ARI reports resources as <extension> / <sip_username> / device-<sip_username>.
    service.asterisk = FakeEndpointList([
        {"technology": "pjsip", "resource": "301", "state": "online"},
        {"technology": "pjsip", "resource": "device-device302", "state": "unavailable"},
        {"technology": "chan_sip", "resource": "302", "state": "online"},   # not pjsip: ignored
    ])

    admin = client.get("/admin/api/device-status")
    assert admin.status_code == 200
    assert {row["id"]: row["registration_status"] for row in admin.json["devices"]} == {
        account_one: "online", account_two: "offline",
    }

    # When ARI cannot be reached the stored status is reported instead of failing.
    service.asterisk = FakeAsterisk()
    assert client.get("/admin/api/device-status").json["devices"] == [
        {"id": account_one, "registration_status": "offline"},
        {"id": account_two, "registration_status": "offline"},
    ]

    # A customer only ever sees their own device.
    customer = client.application.test_client()
    assert customer.post("/admin/login", json={
        "username": "device-one", "password": "device-one-secure-password",
    }).status_code == 200
    scoped = customer.get("/admin/api/device-status")
    assert scoped.status_code == 200
    assert [row["id"] for row in scoped.json["devices"]] == [account_one]
