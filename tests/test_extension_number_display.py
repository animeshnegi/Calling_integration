"""An extension created on a number shows that number in its credentials sheet.

Before the fix, the sheet's "Numbers" row listed only the numbers whose incoming
link points at the extension, so a customer's new extension on +13025550001
showed "None assigned yet" even though it belongs to that line and calls out on it.
"""
import pytest

from app import admin as admin_module
from tests.test_provisioning import admin_client, customer_client, make_app


@pytest.fixture(autouse=True)
def fresh_login_budget():
    """The sign-in limiter is process-wide; each test starts with its own budget."""
    admin_module._LOGIN_BUCKETS.clear()
    yield
    admin_module._LOGIN_BUCKETS.clear()


def _number_for(app, admin, user_id, number="+13025550001", inbound=""):
    response = admin.post("/admin/api/numbers", json={
        "number": number, "provider": "TestProvider", "description": "Line",
        "owner_user_id": user_id, "inbound_extension": inbound, "auto_provision": False,
        "monthly_price": 5, "billing_cycle_day": 1,
    })
    assert response.status_code == 200, response.json


def test_customer_extension_on_a_number_shows_that_number(tmp_path):
    app = make_app(tmp_path)
    admin = admin_client(app)
    customer, user_id = customer_client(app, "meridian")
    _number_for(app, admin, user_id)

    created = customer.post("/admin/api/extensions", json={
        "extension": "102", "number": "+13025550001", "display_name": "Front desk",
    })
    assert created.status_code == 200, created.json

    credentials = customer.get("/admin/api/extensions/102@+13025550001/credentials").json["credentials"]
    assert credentials["number"] == "+13025550001", credentials
    assert "+13025550001" in credentials["numbers"], credentials["numbers"]


def test_sheet_says_which_numbers_ring_the_extension_separately(tmp_path):
    app = make_app(tmp_path)
    admin = admin_client(app)
    customer, user_id = customer_client(app, "acme")
    _number_for(app, admin, user_id, "+13025550002", inbound="")
    customer.post("/admin/api/extensions", json={"extension": "103", "number": "+13025550002"})

    credentials = customer.get("/admin/api/extensions/103@+13025550002/credentials").json["credentials"]
    # Owned by the line, but no incoming call to the number rings it yet.
    assert credentials["numbers"] == ["+13025550002"], credentials["numbers"]
    assert credentials["answers"] == [], credentials["answers"]


def test_incoming_link_is_listed_as_answers_and_still_shows_the_line(tmp_path):
    app = make_app(tmp_path)
    admin = admin_client(app)
    customer, user_id = customer_client(app, "bluewave")
    _number_for(app, admin, user_id, "+13025550003", inbound="")
    customer.post("/admin/api/extensions", json={"extension": "104", "number": "+13025550003"})
    # The number's incoming call now rings 104 on that same line.
    updated = admin.post("/admin/api/numbers", json={
        "number": "+13025550003", "provider": "TestProvider", "owner_user_id": user_id,
        "inbound_extension": "104@+13025550003", "auto_provision": False,
        "monthly_price": 5, "billing_cycle_day": 1,
    })
    assert updated.status_code == 200, updated.json

    credentials = customer.get("/admin/api/extensions/104@+13025550003/credentials").json["credentials"]
    assert credentials["numbers"] == ["+13025550003"], credentials["numbers"]
    assert credentials["answers"] == ["+13025550003"], credentials["answers"]
