"""Auto-provisioning: assigning a number builds a working line, and call flows
exist per number, per extension and per group.

These cover the behaviour the console promises: an administrator assigns a
number and the customer immediately has an extension, SIP credentials, a DID
link and default call flows, all of them editable afterwards.
"""
import re
from pathlib import Path

import pytest

from app import create_app
from app import admin as admin_module
from app.config import Config


@pytest.fixture(autouse=True)
def fresh_login_budget():
    """The sign-in limiter is process-wide by design; each test starts clean."""
    admin_module._LOGIN_BUCKETS.clear()
    yield
    admin_module._LOGIN_BUCKETS.clear()


class FakeAsterisk:
    def health(self):
        return {"system": "Asterisk Test"}

    def create_outbound_call(self, call_id, extension, phone, provider_endpoint, metadata=None):
        return call_id

    def hangup(self, channel_id):
        return None

    def hangup_call(self, employee_channel_id, customer_channel_id):
        return None

    def continue_in_dialplan(self, channel_id, context, extension):
        return None


def make_app(tmp_path: Path):
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
    app.extensions["telephony_service"].asterisk = FakeAsterisk()
    store = app.extensions["settings_store"]
    store.save_provider({
        "name": "TestProvider", "server": "sip.example.com", "port": 5060,
        "username": "user", "password": "provider-password", "transport": "udp",
        "codecs": "ulaw,alaw", "allowed_ips": "198.51.100.10/32",
    })
    return app


class Session:
    """A signed-in client that attaches the CSRF token the API expects."""

    def __init__(self, client):
        self.client = client

    @property
    def csrf(self):
        return self.client.get("/admin/api/state").json["csrf_token"]

    def post(self, path, **kwargs):
        return self.client.post(path, headers={"X-CSRF-Token": self.csrf}, **kwargs)

    def delete(self, path, **kwargs):
        return self.client.delete(path, headers={"X-CSRF-Token": self.csrf}, **kwargs)

    def get(self, path, **kwargs):
        return self.client.get(path, **kwargs)


def admin_client(app):
    client = app.test_client()
    assert client.post("/admin/login", json={"username": "admin", "password": "test-admin-password-1234"}).status_code == 200
    return Session(client)


def customer_client(app, username="tenant"):
    """Create a customer and sign in as them.

    Built through the store rather than /signup: that endpoint is rate limited
    per process, and tests must not spend a shared budget on fixtures.
    """
    store = app.extensions["settings_store"]
    store.save_user({
        "username": username, "password": "customer-password-1234", "role": "user",
        "email": f"{username}@example.com", "full_name": username.title(),
        "company_name": f"{username.title()} Inc", "job_role": "Owner", "phone": "+13025559999",
    })
    user_id = next(row["id"] for row in store.list_users() if row["username"] == username)
    client = app.test_client()
    assert client.post("/login", json={"username": username, "password": "customer-password-1234"}).status_code == 200
    return Session(client), int(user_id)


def identity(extension: str) -> re.Pattern:
    """The SIP username shape: six random letters, an underscore, the extension."""
    return re.compile(rf"^[A-Za-z]{{6}}_{extension}$")


def test_assigning_a_number_provisions_extension_credentials_and_flows(tmp_path):
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "meridian")

    response = admin.post("/admin/api/numbers", json={
        "number": "+13025550001", "provider": "TestProvider", "description": "Meridian main line",
        "owner_user_id": user_id, "inbound_extension": "auto", "auto_provision": True,
        "monthly_price": 5, "billing_cycle_day": 1,
    })
    assert response.status_code == 200, response.json
    provisioned = response.json["provisioned"]
    assert provisioned is not None
    extension = provisioned["extension"]
    assert extension == "101"
    assert identity(extension).match(provisioned["sip_username"]), provisioned["sip_username"]
    assert len(provisioned["sip_password"]) >= 12
    assert provisioned["default_outbound"] is True
    assert provisioned["flows"] == ["number", "extension"]

    # The DID now rings the new extension, which is what makes the line work.
    number = next(row for row in store.list_numbers(user_id) if row["number"] == "+13025550001")
    assert number["inbound_extension"] == extension
    assert number["default_outbound"] == 1
    assert number["owner_user_id"] == user_id

    # The extension belongs to the customer and holds its own credentials.
    extension_row = next(row for row in store.list_extensions(user_id) if row["extension"] == extension)
    assert identity(extension).match(extension_row["sip_username"]), extension_row["sip_username"]
    credentials = store.reveal_extension_credentials(extension, user_id)
    assert credentials["sip_username"] == extension_row["sip_username"]
    assert credentials["sip_password"] == provisioned["sip_password"]
    assert credentials["server"] == "sip.example.com"
    assert credentials["port"] == 5060
    assert credentials["numbers"] == ["+13025550001"]

    # A default flow exists for the number and for the extension.
    number_flow = next(flow for flow in store.list_call_routes(user_id) if flow["phone_number"] == "+13025550001")
    nodes = number_flow["route"]["nodes"]
    # The default is exactly the workflow the customer asked for: ring the
    # provisioned device, and if nobody answers the call ends.
    assert [node["type"] for node in nodes] == ["ring_group"]
    assert nodes[0]["extensions"] == [extension]
    assert all(node.get("configured") for node in nodes)

    extension_flow = next(
        flow for flow in store.list_routing_flows(user_id, target_type="extension") if flow["target"] == extension
    )
    assert extension_flow["route"]["nodes"][0]["extension"] == extension

    # The customer can see all of it in their own state payload.
    state = customer.get("/admin/api/state").json
    assert state["routing_flows"][0]["target"] == extension
    assert state["call_routes"][0]["phone_number"] == "+13025550001"


def test_credentials_reveal_follows_a_linked_device_account(tmp_path):
    """A device account linked to an extension is what Asterisk authenticates,
    so revealing the extension's credentials must report that account's secret."""
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "meridian")
    assign_number(store, user_id, "+13025550001")            # creates extension 101
    extension_identity = next(row["sip_username"] for row in store.list_extensions(user_id) if row["extension"] == "101")
    store.save_sip_account({
        "label": "Reception phone", "sip_username": "reception", "sip_password": "device-secret-9",
        "server": "sip.example.com", "port": 5060, "transport": "udp", "extension": "101",
        "phone_number": "+13025550001",
    }, user_id)

    for client in (admin, customer):
        revealed = client.get("/admin/api/extensions/101/credentials")
        assert revealed.status_code == 200, revealed.data[:200]
        credentials = revealed.json["credentials"]
        assert credentials["sip_username"] == extension_identity
        assert credentials["sip_password"] == "device-secret-9"   # the device's, not the extension's
        assert credentials["registration"] == "device"


def test_each_additional_number_gets_its_own_extension_and_flow(tmp_path):
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "northwind")

    first = admin.post("/admin/api/numbers", json={
        "number": "+13025550011", "provider": "TestProvider", "owner_user_id": user_id,
        "inbound_extension": "auto", "auto_provision": True,
    }).json["provisioned"]
    second = admin.post("/admin/api/numbers", json={
        "number": "+13025550012", "provider": "TestProvider", "owner_user_id": user_id,
        "inbound_extension": "auto", "auto_provision": True,
    }).json["provisioned"]
    assert first["extension"] == "101"
    assert second["extension"] == "102"
    # Caller ID is per extension, so each new line becomes its own extension's
    # default without disturbing the first one.
    assert second["default_outbound"] is True
    assert {row["number"]: row["default_outbound"] for row in store.list_numbers(user_id)} == {
        "+13025550011": 1, "+13025550012": 1,
    }

    flows = {flow["target"] for flow in store.list_routing_flows(user_id, target_type="extension")}
    assert flows == {"101", "102"}
    numbers = store.list_numbers(user_id)
    assert {row["number"]: row["inbound_extension"] for row in numbers} == {
        "+13025550011": "101", "+13025550012": "102",
    }


def test_auto_provision_never_reuses_another_customers_extension(tmp_path):
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    _, first_id = customer_client(app, "alpha")
    _, second_id = customer_client(app, "beta")
    store.save_extension({"extension": "101", "sip_password": "alpha-secret", "active": True}, first_id)

    response = admin.post("/admin/api/numbers", json={
        "number": "+13025550021", "provider": "TestProvider", "owner_user_id": second_id,
        "inbound_extension": "auto", "auto_provision": True,
    })
    assert response.json["provisioned"]["extension"] == "102"
    assert store.get_extension_owner("101") == first_id


def test_customer_created_extension_generates_credentials_and_a_flow(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "acme")

    response = customer.post("/admin/api/extensions", json={"extension": "201", "display_name": "Sales desk"})
    assert response.status_code == 200, response.json
    assert response.json["created"] is True
    credentials = response.json["credentials"]
    assert identity("201").match(credentials["sip_username"]), credentials["sip_username"]
    assert len(credentials["sip_password"]) >= 12

    # Revealable later, and the password is the one that was generated.
    revealed = customer.get("/admin/api/extensions/201/credentials")
    assert revealed.status_code == 200
    assert revealed.json["credentials"]["sip_password"] == credentials["sip_password"]

    # A default call flow was written for it without being asked for.
    flow = next(flow for flow in store.list_routing_flows(user_id, target_type="extension") if flow["target"] == "201")
    assert flow["route"]["nodes"][0]["type"] == "extension"
    assert flow["route"]["nodes"][0]["extension"] == "201"

    # The customer can change the credential afterwards.
    assert customer.post("/admin/api/extensions", json={"extension": "201", "sip_password": "chosen-password-1"}).status_code == 200
    assert customer.get("/admin/api/extensions/201/credentials").json["credentials"]["sip_password"] == "chosen-password-1"


def test_extension_credentials_are_scoped_to_their_owner(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, first_id = customer_client(app, "bluewave")
    store.save_extension({"extension": "301", "sip_password": "bluewave-secret"}, first_id)
    intruder, _ = customer_client(app, "intruder")

    assert intruder.get("/admin/api/extensions/301/credentials").status_code == 404
    assert intruder.get("/admin/api/extensions/999/credentials").status_code == 404
    assert intruder.post("/admin/api/extensions", json={"extension": "301", "sip_password": "stolen-password"}).status_code == 400
    assert app.extensions["settings_store"].reveal_extension_credentials("301", first_id)["sip_password"] == "bluewave-secret"


def test_next_extension_suggestion_tracks_what_is_free(tmp_path):
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "gamma")
    assert admin.get("/admin/api/extensions/next").json["extension"] == "101"
    store.save_extension({"extension": "101", "sip_password": "gamma-secret"}, user_id)
    assert admin.get("/admin/api/extensions/next").json["extension"] == "102"


def test_groups_hold_owned_extensions_and_carry_their_own_flow(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "delta")
    other_admin_client = app.test_client()
    other, other_id = customer_client(app, "epsilon")
    store.save_extension({"extension": "401", "sip_password": "delta-one"}, user_id)
    store.save_extension({"extension": "402", "sip_password": "delta-two"}, user_id)
    store.save_extension({"extension": "501", "sip_password": "epsilon-one"}, other_id)

    response = customer.post("/admin/api/groups", json={"name": "Sales", "members": ["401", "402"], "timeout": 20})
    assert response.status_code == 200, response.json
    group_id = response.json["group_id"]
    group = next(row for row in customer.get("/admin/api/state").json["groups"] if row["id"] == group_id)
    assert group["members"] == ["401", "402"]
    assert group["timeout"] == 20

    # A member must be one of the customer's own extensions.
    assert customer.post("/admin/api/groups", json={"name": "Bad", "members": ["501"]}).status_code == 400
    # Names are unique per customer.
    assert customer.post("/admin/api/groups", json={"name": "Sales", "members": ["401"]}).status_code == 400

    # A group can be the ring destination of a flow, and has a flow of its own.
    save = customer.post("/admin/api/call-routes", json={
        "target_type": "group", "target": str(group_id), "name": "Sales flow",
        "route": {"nodes": [
            {"type": "ring_group", "group_id": str(group_id), "extensions": ["401", "402"], "timeout": 20,
             "label": "Sales · 20s", "configured": True},
            {"type": "voicemail", "mailbox": "401", "label": "Voicemail 401", "configured": True},
        ]},
    })
    assert save.status_code == 200, save.json
    flow = next(flow for flow in store.list_routing_flows(user_id, target_type="group") if flow["target"] == str(group_id))
    assert flow["route"]["nodes"][0]["group_id"] == str(group_id)

    # A flow cannot ring extensions that are not in the group it names.
    mismatch = customer.post("/admin/api/call-routes", json={
        "target_type": "group", "target": str(group_id),
        "route": {"nodes": [{"type": "ring_group", "group_id": str(group_id), "extensions": ["501"], "timeout": 20}]},
    })
    assert mismatch.status_code == 400
    # Nor can a customer name somebody else's group.
    foreign = customer.post("/admin/api/call-routes", json={
        "target_type": "group", "target": "99999",
        "route": {"nodes": [{"type": "ring_group", "extensions": ["401"], "timeout": 20}]},
    })
    assert foreign.status_code == 400

    # Deleting the group removes its flow but keeps the extensions.
    assert customer.delete(f"/admin/api/groups/{group_id}").status_code == 200
    assert store.list_routing_flows(user_id, target_type="group") == []
    assert {row["extension"] for row in store.list_extensions(user_id)} == {"401", "402"}


def test_deleting_an_extension_cleans_up_flows_and_groups(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "zeta")
    store.save_extension({"extension": "601", "sip_password": "zeta-one"}, user_id)
    store.save_extension({"extension": "602", "sip_password": "zeta-two"}, user_id)
    group_id = customer.post("/admin/api/groups", json={"name": "Support", "members": ["601", "602"]}).json["group_id"]

    assert customer.delete("/admin/api/extensions/601").status_code == 200
    assert store.list_routing_flows(user_id, target_type="extension") and all(
        flow["target"] != "601" for flow in store.list_routing_flows(user_id, target_type="extension")
    )
    group = next(row for row in store.list_groups(user_id) if row["id"] == group_id)
    assert group["members"] == ["602"]


def test_extension_flows_are_validated_and_tenant_isolated(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "theta")
    _, other_id = customer_client(app, "iota")
    store.save_extension({"extension": "701", "sip_password": "theta-secret"}, user_id)
    store.save_extension({"extension": "801", "sip_password": "iota-secret"}, other_id)

    own = customer.post("/admin/api/call-routes", json={
        "target_type": "extension", "target": "701",
        "route": {"nodes": [{"type": "simultaneous", "extensions": ["701"], "timeout": 25, "configured": True}]},
    })
    assert own.status_code == 200

    foreign_ring = customer.post("/admin/api/call-routes", json={
        "target_type": "extension", "target": "701",
        "route": {"nodes": [{"type": "simultaneous", "extensions": ["801"], "timeout": 25, "configured": True}]},
    })
    assert foreign_ring.status_code == 400

    foreign_target = customer.post("/admin/api/call-routes", json={
        "target_type": "extension", "target": "801",
        "route": {"nodes": [{"type": "extension", "extension": "801", "configured": True}]},
    })
    assert foreign_target.status_code == 400

    bad_timeout = customer.post("/admin/api/call-routes", json={
        "target_type": "extension", "target": "701",
        "route": {"nodes": [{"type": "simultaneous", "extensions": ["701"], "timeout": 400}]},
    })
    assert bad_timeout.status_code == 400


def test_a_device_account_cannot_take_an_extension_identity(tmp_path):
    """The number a device authenticates with stays unique and unchangeable."""
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "meridian")
    _, other_id = customer_client(app, "northwind")
    assign_number(store, user_id, "+13025550001")          # extension 101

    # Linked to an extension: the account takes that extension's generated
    # identity, whatever the caller asked for.
    identity_of_101 = next(row["sip_username"] for row in store.list_extensions(user_id) if row["extension"] == "101")
    account_id = store.save_sip_account({
        "label": "Desk phone", "sip_username": "reception", "sip_password": "device-secret-1",
        "server": "sip.example.com", "extension": "101",
    }, user_id)
    assert store.reveal_extension_credentials("101", user_id)["sip_username"] == identity_of_101
    assert next(row for row in store.list_sip_accounts(user_id) if row["id"] == account_id)["sip_username"] == identity_of_101

    # Unlinked accounts may be named - but never after an extension's identity,
    # nor its number, and never twice, whichever customer owns them.
    assert store.save_sip_account({
        "label": "Lobby phone", "sip_username": "lobby", "sip_password": "device-secret-2",
        "server": "sip.example.com",
    }, user_id) is not None
    with pytest.raises(ValueError):
        store.save_sip_account({
            "label": "Sneaky", "sip_username": "101", "sip_password": "device-secret-3",
            "server": "sip.example.com",
        }, other_id)
    with pytest.raises(ValueError):
        store.save_sip_account({
            "label": "Impostor", "sip_username": identity_of_101, "sip_password": "device-secret-5",
            "server": "sip.example.com",
        }, other_id)
    with pytest.raises(ValueError):
        store.save_sip_account({
            "label": "Duplicate", "sip_username": "lobby", "sip_password": "device-secret-4",
            "server": "sip.example.com",
        }, other_id)


def test_administrators_edit_a_customers_call_flows(tmp_path):
    """An administrator answers the phone for their customers: they can rewrite a
    customer's flows, but a flow always stays inside the customer that owns it."""
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "kappa")
    other, other_id = customer_client(app, "lambda")
    store.save_extension({"extension": "901", "sip_password": "kappa-secret"}, user_id)
    store.save_extension({"extension": "902", "sip_password": "lambda-secret"}, other_id)
    assign_number(store, user_id, "+13025550001")
    assign_number(store, other_id, "+13025550002")

    # The administrator re-routes the customer's number to that customer's extension.
    number_flow = admin.post("/admin/api/call-routes", json={
        "target_type": "number", "phone_number": "+13025550001",
        "route": {"nodes": [{"type": "extension", "extension": "901", "configured": True}]},
    })
    assert number_flow.status_code == 200, number_flow.json
    extension_flow = admin.post("/admin/api/call-routes", json={
        "target_type": "extension", "owner_user_id": user_id, "target": "901",
        "route": {"nodes": [{"type": "extension", "extension": "901", "configured": True}]},
    })
    assert extension_flow.status_code == 200, extension_flow.json
    group = admin.post("/admin/api/groups", json={
        "owner_user_id": user_id, "name": "Front desk", "members": ["901"], "timeout": 30,
    })
    assert group.status_code == 200, group.json
    assert admin.post("/admin/api/call-routes", json={
        "target_type": "group", "owner_user_id": user_id, "target": str(group.json["group_id"]),
        "route": {"nodes": [{"type": "extension", "extension": "901", "configured": True}]},
    }).status_code == 200

    # The work landed on the customer the flow belongs to, not on the operator.
    kappa = admin.get(f"/admin/api/customers/{user_id}").json
    assert {row["extension"] for row in kappa["extensions"]} == {"101", "901"}   # 101 came with the number
    assert {row["target"] for row in kappa["routing_flows"]} >= {"901", str(group.json["group_id"])}
    assert [row["name"] for row in kappa["groups"]] == ["Front desk"]
    lambda_ = admin.get(f"/admin/api/customers/{other_id}").json
    untouched = {row["target"] for row in lambda_["routing_flows"]}
    assert "901" not in untouched and "902" in untouched   # 102 came with their own number
    assert {row["phone_number"] for row in lambda_["call_routes"]} == {"+13025550002"}
    assert lambda_["groups"] == []

    # Naming somebody else's customer cannot move a flow: the target decides who
    # owns it, so this write lands on 901's own customer, never on the customer
    # the request tried to name.
    assert admin.post("/admin/api/call-routes", json={
        "target_type": "extension", "owner_user_id": other_id, "target": "901",
        "route": {"nodes": [{"type": "extension", "extension": "901", "configured": True}]},
    }).status_code == 200
    assert {row["target"] for row in admin.get(f"/admin/api/customers/{other_id}").json["routing_flows"]} == untouched
    assert admin.post("/admin/api/groups", json={"name": "Nowhere", "members": ["902"]}).status_code == 400

    # The customer keeps the same rights over their own flows.
    assert customer.post("/admin/api/call-routes", json={
        "target_type": "extension", "target": "901",
        "route": {"nodes": [{"type": "extension", "extension": "901", "configured": True}]},
    }).status_code == 200
    assert other.post("/admin/api/call-routes", json={
        "target_type": "number", "phone_number": "+13025550001", "target": "+13025550001",
        "route": {"nodes": [{"type": "extension", "extension": "901", "configured": True}]},
    }).status_code == 400   # not their number


def test_provisioning_reports_a_number_that_already_has_an_extension(tmp_path):
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "lambda")

    store.save_number({"number": "+13025550031", "provider": "TestProvider", "owner_user_id": user_id, "inbound_extension": ""})
    with pytest.raises(ValueError):
        store.provision_number(user_id, "+13025550032")
    first = store.provision_number(user_id, "+13025550031")
    assert first["extension"] == "101"
    with pytest.raises(ValueError):
        store.provision_number(user_id, "+13025550031")


def test_default_route_shapes_match_the_canvas(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    number_route = store.default_number_route(["101"], voicemail="101")
    extension_route = store.default_extension_route("101", voicemail=True)
    assert [node["type"] for node in number_route["nodes"]] == ["ring_group", "voicemail"]
    assert [node["type"] for node in extension_route["nodes"]] == ["extension", "voicemail"]
    # Without voicemail the default flow is a single ring step: no answer ends
    # the call rather than parking the caller.
    assert [node["type"] for node in store.default_number_route(["101"])["nodes"]] == ["ring_group"]
    assert [node["type"] for node in store.default_extension_route("101")["nodes"]] == ["extension"]
    # Saving what the builder produced must pass the same validation the UI does.
    _, user_id = customer_client(app, "munich")
    store.save_extension({"extension": "111", "sip_password": "mu-secret"}, user_id)
    assert store.save_routing_flow(user_id, {"route": store.default_extension_route("111")}, target_type="extension", target="111")
    with pytest.raises(ValueError):
        store.save_routing_flow(user_id, {"route": {"nodes": [{"type": "extension", "extension": "999"}]}}, target_type="extension", target="111")


# --------------------------------------------------------------------------
# The default call workflow: ring the provisioned device(s), and if nobody
# answers the call simply ends. Extension-specific numbers ring only their own
# device; a customer's main line rings every device they have.
# --------------------------------------------------------------------------

class RingingAsterisk(FakeAsterisk):
    """Records what the engine rings and what it tears down."""

    def __init__(self, live=None):
        self.legs = []
        self.hangups = []
        self.dialplan = []
        self.bridges = []
        self.live = list(live or [])

    def create_bridge(self, bridge_id):
        self.bridges.append(bridge_id)
        return bridge_id

    def add_channel_to_bridge(self, bridge_id, channel_id):
        return None

    def start_bridge_recording(self, *args, **kwargs):
        return None

    def create_inbound_employee_leg(self, call_id, extension, customer_channel_id, index=0):
        leg = f"{call_id}-employee" if index == 0 else f"{call_id}-employee-{index}"
        self.legs.append((leg, extension))
        self.live.append(leg)
        return leg

    def hangup(self, channel_id):
        self.hangups.append(channel_id)
        if channel_id in self.live:
            self.live.remove(channel_id)

    def list_channels(self):
        return [{"id": channel_id} for channel_id in self.live]

    def continue_in_dialplan(self, channel_id, context, extension):
        self.dialplan.append((channel_id, context, extension))

    def hangup_call(self, employee_channel_id, customer_channel_id):
        self.hangup(employee_channel_id)
        self.hangup(customer_channel_id)


def assign_number(store, user_id, number):
    """What the console does: the DID is assigned to the customer first, and
    provisioning then builds the extension, credentials and flows on top."""
    store.save_number({
        "number": number, "provider": "TestProvider", "description": "Assigned line",
        "inbound_extension": "", "owner_user_id": user_id, "monthly_price": 5, "active": True,
    })
    return store.provision_number(user_id, number)


def ring_engine(app):
    """An app whose Asterisk client records the ringing plan."""
    asterisk = RingingAsterisk()
    service = app.extensions["telephony_service"]
    service.asterisk = asterisk
    return service, asterisk


def inbound(service, number, extension):
    service.handle_ari_event({
        "type": "StasisStart", "args": ["inbound", number, extension],
        "channel": {"id": f"carrier-{number}", "state": "Up", "caller": {"number": "+919999999999"}},
    })
    return service.store.all()[-1]


def test_main_line_rings_every_device_the_customer_has(tmp_path):
    """Incoming call -> main number -> every phone rings (answer or end)."""
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "meridian")
    assign_number(store, user_id, "+13025550001")
    assign_number(store, user_id, "+13025550002")

    main = store.primary_number(user_id)
    assert main == "+13025550001"
    plan = store.inbound_plan(main)
    assert plan["destinations"] == ["101", "102"], plan
    assert plan["voicemail"] == ""  # nobody answers -> the call ends

    service, asterisk = ring_engine(app)
    call = inbound(service, main, "101")
    assert asterisk.legs == [
        (f"{call.call_id}-employee", "101"),
        (f"{call.call_id}-employee-1", "102"),
    ], asterisk.legs
    stored = service.store.get(call.call_id)
    assert stored.employee_channel_id == f"{call.call_id}-employee"
    assert stored.employee_channel_ids == f"{call.call_id}-employee|101,{call.call_id}-employee-1|102"


def test_extension_specific_number_rings_only_that_extension(tmp_path):
    """A number tied to one extension rings that extension, not the whole office."""
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "bluewave")
    assign_number(store, user_id, "+13025550001")          # 101, the main line
    second = assign_number(store, user_id, "+13025550002")  # 102
    assert store.inbound_plan("+13025550002")["destinations"] == [second["extension"]]

    service, asterisk = ring_engine(app)
    inbound(service, "+13025550002", second["extension"])
    assert [extension for _, extension in asterisk.legs] == [second["extension"]]


def test_answering_one_device_cancels_the_other_legs(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "northwind")
    assign_number(store, user_id, "+13025550001")
    assign_number(store, user_id, "+13025550002")

    service, asterisk = ring_engine(app)
    call = inbound(service, "+13025550001", "101")
    service.handle_ari_event({
        "type": "ChannelStateChange", "channel": {"id": f"{call.call_id}-employee-1", "state": "Up"},
    })
    updated = service.store.get(call.call_id)
    assert updated.employee_channel_id == f"{call.call_id}-employee-1"
    assert updated.extension == "102"          # the phone that picked up
    assert updated.answered is True            # and the call connected
    assert f"{call.call_id}-employee" in asterisk.hangups  # the other phone stops ringing


def test_no_answer_ends_the_call_without_voicemail(tmp_path):
    """Ring -> answer = connect, no answer = disconnect. That is the whole default."""
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "acme")
    assign_number(store, user_id, "+13025550001")

    service, asterisk = ring_engine(app)
    call = inbound(service, "+13025550001", "101")
    service.handle_ari_event({
        "type": "ChannelDestroyed", "channel": {"id": f"{call.call_id}-employee"},
    })
    assert asterisk.dialplan == []
    assert "carrier-+13025550001" in asterisk.hangups
    assert service.store.get(call.call_id).status == "failed"


def test_a_flow_that_ends_in_voicemail_still_takes_a_message(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "meridian")
    assign_number(store, user_id, "+13025550001")
    store.save_call_route(user_id, {
        "phone_number": "+13025550001", "name": "Main call flow", "active": True,
        "route": {"nodes": [
            {"type": "ring_group", "extensions": ["101"], "timeout": 20, "configured": True},
            {"type": "voicemail", "mailbox": "101", "configured": True},
        ]},
    })
    assert store.inbound_plan("+13025550001")["voicemail"] == "101"

    service, asterisk = ring_engine(app)
    call = inbound(service, "+13025550001", "101")
    service.handle_ari_event({
        "type": "ChannelDestroyed", "channel": {"id": f"{call.call_id}-employee"},
    })
    assert asterisk.dialplan == [("carrier-+13025550001", "voicemail-inbound", "101")]


def test_business_hours_skip_ringing_outside_the_schedule(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "munich")
    assign_number(store, user_id, "+13025550001")
    store.save_call_route(user_id, {
        "phone_number": "+13025550001", "name": "Main call flow", "active": True,
        "route": {"nodes": [
            {"type": "business_hours", "start": "00:00", "end": "00:01", "days": [1], "configured": True},
            {"type": "ring_group", "extensions": ["101"], "timeout": 20, "configured": True},
            {"type": "voicemail", "mailbox": "101", "configured": True},
        ]},
    })
    plan = store.inbound_plan("+13025550001")
    assert plan["outside_hours"] is True
    assert plan["destinations"] == []
    assert plan["voicemail"] == "101"


def test_adding_an_extension_extends_the_main_line(tmp_path):
    """The main line keeps ringing every device, without touching edited flows."""
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "meridian")
    assign_number(store, user_id, "+13025550001")
    store.save_extension({"extension": "102", "sip_password": "second-secret", "active": True}, user_id)

    assert store.inbound_plan("+13025550001")["destinations"] == ["101", "102"]

    # A flow the customer designed is never rewritten.
    store.save_call_route(user_id, {
        "phone_number": "+13025550001", "name": "Custom", "active": True,
        "route": {"nodes": [{"type": "ring_group", "extensions": ["101"], "timeout": 15, "configured": True}]},
    })
    store.save_extension({"extension": "105", "sip_password": "third-secret", "active": True}, user_id)
    assert store.inbound_plan("+13025550001")["destinations"] == ["101"]


def test_a_stale_main_line_default_catches_up_with_later_devices(tmp_path):
    """A device added while the platform was not looking is still picked up."""
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "meridian")
    assign_number(store, user_id, "+13025550001")
    store.save_extension({"extension": "102", "sip_password": "second-secret", "active": True}, user_id)
    # The main line still lists only 101 - as a flow provisioned before 102 existed
    # would. Adding the next device must bring all of them in.
    store.save_call_route(user_id, {
        "phone_number": "+13025550001", "name": "Main call flow", "active": True,
        "route": store.default_number_route(["101"]),
    })
    store.save_extension({"extension": "103", "sip_password": "third-secret", "active": True}, user_id)
    assert store.inbound_plan("+13025550001")["destinations"] == ["101", "102", "103"]


def test_sip_username_is_generated_and_cannot_be_changed(tmp_path):
    """Six random letters, an underscore, the extension - and never a caller's choice."""
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "meridian")
    store.save_extension({"extension": "101", "sip_password": "chosen", "sip_username": "meridian-softphone"}, user_id)
    row = next(item for item in store.list_extensions(user_id) if item["extension"] == "101")
    assert identity("101").match(row["sip_username"]), row["sip_username"]
    assert row["sip_username"] != "101" and row["sip_username"] != "meridian-softphone"

    # An edit that tries to rename it is ignored, and so is one that renames it to
    # a shape the platform did not mint: the identity a registered phone uses
    # cannot drift.
    store.save_extension({"extension": "101", "sip_password": "chosen", "sip_username": "renamed"}, user_id)
    row = next(item for item in store.list_extensions(user_id) if item["extension"] == "101")
    assert identity("101").match(row["sip_username"]), row["sip_username"]
    assert store.reveal_extension_credentials("101", user_id)["sip_username"] == row["sip_username"]

    # Two extensions never share a username, every identity ends with its own
    # extension, and the letters are random rather than derived from it.
    store.save_extension({"extension": "102", "sip_password": "chosen"}, user_id)
    usernames = {item["extension"]: item["sip_username"] for item in store.list_extensions(user_id)}
    assert len(set(usernames.values())) == len(usernames)
    assert all(value.endswith(f"_{key}") for key, value in usernames.items())
    assert usernames["101"] == row["sip_username"]              # editing did not rename it
    assert usernames["101"] != f"101" and not usernames["101"].startswith("meridian")

    # A row from an older release is normalised on the next startup rather than
    # left in a shape the credential sheet does not describe.
    with store._connect() as db:  # noqa: SLF001 - asserting the schema, not the API
        db.execute("UPDATE extensions SET sip_username=? WHERE extension=?", ("101", "101"))
        db.execute("UPDATE extensions SET sip_username=? WHERE extension=?", ("102_old", "102"))
        assert store.normalise_sip_usernames(db) == 2
        indexes = [row["name"] for row in db.execute("PRAGMA index_list(extensions)").fetchall()]
    assert "idx_extensions_sip_username" in indexes
    for item in store.list_extensions(user_id):
        assert identity(item["extension"]).match(item["sip_username"]), item


def test_the_effective_password_is_the_one_that_rotates(tmp_path):
    """Changing the password changes what the phone actually registers with."""
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "meridian")
    assign_number(store, user_id, "+13025550001")            # extension 101
    before = store.reveal_extension_credentials("101", user_id)

    # A chosen password is stored as given, and a blank one is generated.
    chosen = store.set_extension_password("101", "handset-secret-42", user_id)
    assert chosen["password"] == "handset-secret-42" and chosen["source"] == "extension"
    assert store.reveal_extension_credentials("101", user_id)["sip_password"] == "handset-secret-42"
    generated = store.set_extension_password("101", "", user_id)
    assert generated["password"] != "handset-secret-42" and len(generated["password"]) >= 12
    assert store.reveal_extension_credentials("101", user_id)["sip_password"] == generated["password"]

    # With a device account linked, that account is the live credential, so the
    # rotation has to land there - the console must never show a stale secret.
    store.save_sip_account({
        "label": "Reception phone", "sip_username": "reception", "sip_password": "device-secret-9",
        "server": "sip.example.com", "extension": "101",
    }, user_id)
    rotated = store.set_extension_password("101", "rotated-device-secret", user_id)
    assert rotated["source"] == "device"
    assert store.reveal_extension_credentials("101", user_id)["sip_password"] == "rotated-device-secret"

    # An empty password on a device keeps the device's name and its server.
    account = store.list_sip_accounts(user_id, include_password=True)[0]
    assert account["sip_username"] == before["sip_username"] and account["server"] == "sip.example.com"

    # Only the owner (or an administrator) may rotate it, and the shape is checked.
    _, other_id = customer_client(app, "northwind")
    with pytest.raises(ValueError):
        store.set_extension_password("101", "not-mine", other_id)
    with pytest.raises(ValueError):
        store.set_extension_password("101", "bad\npassword", user_id)
    assert store.set_extension_password("101", "admin-reset", None)["password"] == "admin-reset"


def test_the_generated_identity_and_password_have_the_documented_shape(tmp_path):
    """The username is six letters and the extension; the password is strong.

    Both are generated, never typed, so the shape is a promise the platform makes
    to whoever reads them off the credential sheet and keys them into a phone.
    """
    app = make_app(tmp_path)
    store = app.extensions["settings_store"]
    usernames = set()
    for extension in ("101", "102", "201", "301"):
        username = store.generate_sip_username(extension)
        assert re.fullmatch(rf"[A-Z]{{6}}_{extension}", username), username
        usernames.add(username)
    assert len(usernames) == 4                                               # the letters are random too
    assert len({store.generate_sip_username("101").split("_")[0] for _ in range(40)}) > 30

    for _ in range(40):
        secret = store.generate_sip_password()
        assert len(secret) >= 12
        assert re.search(r"[A-Z]", secret) and re.search(r"[a-z]", secret)
        assert re.search(r"[0-9]", secret) and re.search(r"[^A-Za-z0-9]", secret)
        # The generated Asterisk configuration rejects these characters outright.
        assert not set(secret) & set(";#\n\r ")
    assert len({store.generate_sip_password() for _ in range(20)}) == 20
    assert len(store.generate_sip_password(24)) == 24
    assert len(store.generate_sip_password(4)) == 12            # never shorter than the floor


def test_a_generated_password_renders_into_the_asterisk_config(tmp_path):
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    _, user_id = customer_client(app, "meridian")
    provisioned = admin.post("/admin/api/numbers", json={
        "number": "+13025550009", "provider": "TestProvider", "owner_user_id": user_id,
        "inbound_extension": "auto", "auto_provision": True,
    }).json["provisioned"]
    from app.telephony_config import TelephonyConfigSync

    rendered = TelephonyConfigSync(store, None, tmp_path / "pjsip.conf").render_pjsip()
    assert f"password={provisioned['sip_password']}" in rendered
    assert f"username={provisioned['sip_username']}" in rendered


def test_the_layout_self_check_is_served_to_signed_in_accounts_only(tmp_path):
    """The geometry of the drawer can only be measured in a real browser, so the
    console ships a self-check page. It must not leak anything about a customer."""
    app = make_app(tmp_path)
    admin = admin_client(app)
    customer, _ = customer_client(app, "meridian")

    for client in (admin, customer):
        page = client.get("/console-check")
        assert page.status_code == 200
        body = page.get_data(as_text=True)
        assert "Console layout check" in body
        assert "ws-scroll" in body and "platform-recording" in body      # it measures the real thing
        assert "{{" not in body                                          # nothing left to render
        # A diagnostic page, not a view of anybody's account.
        assert "meridian" not in body.lower().replace("console layout check", "")

    anonymous = make_app(tmp_path).test_client()
    assert anonymous.get("/console-check").status_code in (302, 401)


def test_the_platform_recording_switch_is_the_administrators_and_it_vetoes(tmp_path):
    """Round 5 removed the global controls; the administrator asked for the
    on/off switch back. It is one switch, and off means off everywhere."""
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "meridian")
    assign_number(store, user_id, "+13025550001")            # extension 101

    # A fresh install allows recording - the switch is a veto, not a gate - and
    # still records nothing, because no device has opted in.
    assert store.recording_platform_enabled() is True
    assert app.extensions["telephony_service"]._recording_settings("101")["enabled"] is False
    assert customer.get("/admin/api/state").json["recording_platform_enabled"] is True

    # The customer's own per-device switch is what starts recording.
    switched = customer.post("/admin/api/profile/recording", json={"enabled": True})
    assert switched.status_code == 200
    assert next(row for row in store.list_extensions(user_id) if row["extension"] == "101")["recording_enabled"] == 1
    assert app.extensions["telephony_service"]._recording_settings("101")["enabled"] is True

    # The administrator's switch stops it everywhere, immediately, and the
    # customer's own choice is kept for when it goes back on.
    assert admin.post("/admin/api/settings", json={"recording_enabled": False}).status_code == 200
    assert store.recording_platform_enabled() is False
    assert app.extensions["telephony_service"]._recording_settings("101")["enabled"] is False
    assert customer.get("/admin/api/state").json["recording_platform_enabled"] is False
    assert next(row for row in store.list_extensions(user_id) if row["extension"] == "101")["recording_enabled"] == 1

    # Back on: the device records again without the customer doing anything.
    assert admin.post("/admin/api/settings", json={"recording_enabled": True}).status_code == 200
    assert app.extensions["telephony_service"]._recording_settings("101")["enabled"] is True

    # A device that never opted in still does not record, either way.
    customer.post("/admin/api/profile/recording", json={"enabled": False})
    assert app.extensions["telephony_service"]._recording_settings("101")["enabled"] is False

    # Customers and extension users never get to flip the platform switch.
    assert customer.post("/admin/api/settings", json={"recording_enabled": True}).status_code in (400, 403)
    assert app.test_client().post("/admin/api/settings", json={"recording_enabled": True}).status_code in (302, 401)

    # Only real switches count: anything else is refused, and the switch stays put.
    assert admin.post("/admin/api/settings", json={"recording_enabled": "maybe"}).status_code == 400
    assert store.recording_platform_enabled() is True


def test_the_registration_address_is_the_platforms_own_not_the_carriers(tmp_path):
    """A device registers with this deployment, so the administrator's address is
    what the credential sheet shows - not the carrier trunk the platform dials."""
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "meridian")
    assign_number(store, user_id, "+13025550001")            # extension 101

    # Before anything is configured the carrier is what the platform used to say.
    assert store.reveal_extension_credentials("101", user_id)["server"] == "sip.example.com"

    saved = admin.post("/admin/api/settings", json={"service_host": "pbx.meridian-voice.test", "service_sip_port": "5080"})
    assert saved.status_code == 200, saved.json
    for client in (admin, customer):
        credentials = client.get("/admin/api/extensions/101/credentials").json["credentials"]
        assert credentials["server"] == "pbx.meridian-voice.test"
        assert credentials["port"] == 5080
        assert credentials["registration_address"] == "pbx.meridian-voice.test:5080"
        assert credentials["api_base"] == "https://pbx.meridian-voice.test"
        assert credentials["managed_address"] is True

    # The state payload carries the same address to both consoles.
    assert customer.get("/admin/api/state").json["service_address"]["sip"] == "pbx.meridian-voice.test:5080"

    # An IP address is just as valid as a subdomain.
    assert admin.post("/admin/api/settings", json={"service_host": "203.0.113.10"}).status_code == 200
    assert customer.get("/admin/api/extensions/101/credentials").json["credentials"]["server"] == "203.0.113.10"

    # A scheme, a path or a port is a mistake that would break every phone at once.
    for bad in ("https://pbx.test", "pbx.test/sip", "pbx.test:5060", "pbx test", "pbx..test"):
        refused = admin.post("/admin/api/settings", json={"service_host": bad})
        assert refused.status_code == 400, (bad, refused.json)
    assert admin.post("/admin/api/settings", json={"service_sip_port": "70000"}).status_code == 400

    # Only the platform's administrators own this address.
    refused = customer.post("/admin/api/settings", json={"service_host": "customer.example"})
    assert refused.status_code in (400, 403)

    # Clearing it falls back to the host the console is being read from.
    assert admin.post("/admin/api/settings", json={"service_host": ""}).status_code == 200
    fallback = customer.get("/admin/api/extensions/101/credentials").json["credentials"]
    assert fallback["server"] == "localhost" and fallback["managed_address"] is False


def test_the_documentation_page_names_this_deployment(tmp_path):
    app = make_app(tmp_path)
    admin = admin_client(app)
    _, user_id = customer_client(app, "meridian")
    page = admin.get("/documentation")
    assert page.status_code == 200
    assert b"{{API_BASE}}" not in page.data and b"{{SIP_HOST}}" not in page.data   # filled in
    assert b"https://localhost/api/v1/calls" in page.data

    admin.post("/admin/api/settings", json={"service_host": "voice.acme.test"})
    page = admin.get("/documentation").data.decode()
    assert "https://voice.acme.test/api/v1/calls" in page
    assert "voice.acme.test:5060" in page
    assert "KUDGTE_101" in page
    assert 'href="/admin"' in page

    # It stays behind the sign-in wall.
    anonymous = make_app(tmp_path).test_client()
    assert anonymous.get("/documentation").status_code in (302, 401)


def test_the_password_endpoint_rotates_the_credential_a_phone_uses(tmp_path):
    """The console's "change password" acts on the credential Asterisk reads."""
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    customer, user_id = customer_client(app, "meridian")
    assign_number(store, user_id, "+13025550001")            # extension 101
    assert next(row["sip_username"] for row in store.list_extensions(user_id) if row["extension"] == "101").endswith("_101")

    # A password the customer chose is stored and revealed afterwards.
    response = customer.post("/admin/api/extensions/101/password", json={"password": "handset-secret-42"})
    assert response.status_code == 200, response.json
    assert response.json["sip_password"] == "handset-secret-42" and response.json["source"] == "extension"
    assert store.reveal_extension_credentials("101", user_id)["sip_password"] == "handset-secret-42"

    # A blank request generates a strong one rather than clearing it.
    generated = customer.post("/admin/api/extensions/101/password", json={})
    assert generated.status_code == 200
    secret = generated.json["sip_password"]
    assert len(secret) >= 12 and secret != "handset-secret-42"
    assert store.reveal_extension_credentials("101", user_id)["sip_password"] == secret

    # With a device account linked, that account is what authenticates.
    store.save_sip_account({
        "label": "Reception phone", "sip_username": "reception", "sip_password": "device-secret-9",
        "server": "sip.example.com", "extension": "101",
    }, user_id)
    linked = customer.post("/admin/api/extensions/101/password", json={"password": "rotated-device-secret"})
    assert linked.status_code == 200 and linked.json["source"] == "device"
    assert store.reveal_extension_credentials("101", user_id)["sip_password"] == "rotated-device-secret"

    # Another customer cannot reach it, and neither can an anonymous session.
    _, other_id = customer_client(app, "northwind")
    other = app.test_client()
    assert other.post("/login", json={"username": "northwind", "password": "customer-password-1234"}).status_code == 200
    other_token = other.get("/admin/api/state").json["csrf_token"]
    refusal = other.post("/admin/api/extensions/101/password", json={"password": "not-mine"},
                         headers={"X-CSRF-Token": other_token})
    assert refusal.status_code == 400 and "not found" in refusal.json["error"], refusal.json
    assert app.test_client().post("/admin/api/extensions/101/password", json={"password": "anon"}).status_code in (302, 401)

    # An administrator may rotate it on the customer's behalf, and the change is
    # recorded in that customer's activity feed.
    assert admin.post("/admin/api/extensions/101/password", json={"password": "operator-reset"}).status_code == 200
    assert store.reveal_extension_credentials("101", user_id)["sip_password"] == "operator-reset"
    assert any(row["action"] == "extension.password_rotated" for row in store.list_activity(user_id))


def test_administrator_accounts_cannot_be_deleted(tmp_path):
    """The panel offers no delete action for an admin, and the API refuses it too."""
    app = make_app(tmp_path)
    admin = admin_client(app)
    store = app.extensions["settings_store"]
    second = store.save_user({
        "username": "second-admin", "password": "second-admin-password", "role": "admin",
        "email": "second@example.test", "full_name": "Second", "company_name": "Platform",
    })
    victim = next(row["id"] for row in store.list_users() if row["username"] == "second-admin")
    assert store.get_user(second) is not None

    refusal = admin.delete(f"/admin/api/users/{victim}")
    assert refusal.status_code == 400, refusal.json
    assert "cannot be deleted" in refusal.json["error"]
    assert store.get_user(victim) is not None                    # still there
    with pytest.raises(ValueError):
        store.delete_user(victim, 999)

    # Customers are still removable, which is the action this guard must not block.
    customer, user_id = customer_client(app, "meridian")
    assert admin.delete(f"/admin/api/users/{user_id}").status_code == 200
    assert store.get_user(user_id) is None


def test_administrators_cannot_place_calls(tmp_path):
    """The administrator manages the platform; dialling is the customer's action."""
    app = make_app(tmp_path)
    admin = admin_client(app)
    response = admin.post("/admin/api/calls", json={"phone": "+13025550123", "extension": "101"})
    assert response.status_code == 403, response.json
    assert "do not place calls" in response.json["error"]
