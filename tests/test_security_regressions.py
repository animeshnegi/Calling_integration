"""Adversarial tests for public account and tenant-resource boundaries."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from tests.test_api import app_client
from app.accounts import create_customer, update_user_as_admin
from app.database import Database
from app.idempotency import reserve
from app.models import Call, CallStore
from app.rate_limits import RateLimiter
from app.services import TelephonyService
from app import webhook_transport


def customer(store, name):
    return store.save_user({"username": name, "email": name + "@example.com", "password": "customer-password-123", "role": "user"})


@pytest.mark.parametrize("target", ["customer", "admin"])
def test_signup_cannot_update_accounts(tmp_path, target):
    client = app_client(tmp_path)
    store = client.application.extensions["settings_store"]
    victim = customer(store, "victim") if target == "customer" else next(u["id"] for u in store.list_users() if u["role"] == "admin")
    before = store.get_user(victim)
    with store._connect() as db:
        password_before = db.execute("SELECT password_hash FROM admin_users WHERE id=?", (victim,)).fetchone()[0]
    payload = {"id": victim, "username": "attacker", "email": "attacker@example.com", "password": "attacker-password-123", "full_name": "Attacker", "company_name": "Bad", "job_role": "Owner", "phone": "+13025550199"}
    assert client.post("/signup", json=payload).status_code == 400
    assert store.get_user(victim) == before
    with store._connect() as db:
        assert db.execute("SELECT password_hash FROM admin_users WHERE id=?", (victim,)).fetchone()[0] == password_before
    assert client.post("/login", json={"username": "attacker", "password": payload["password"]}).status_code == 401
    for field in ("role", "active", "extension"):
        forbidden = {k: v for k, v in payload.items() if k != "id"}
        forbidden[field] = "admin"
        assert client.post("/signup", json=forbidden).status_code == 400


def test_admin_account_operation_checks_actor(tmp_path):
    client = app_client(tmp_path)
    store = client.application.extensions["settings_store"]
    actor = customer(store, "actor")
    with pytest.raises(ValueError, match="Administrator"):
        update_user_as_admin(store, {}, actor)
    with pytest.raises(ValueError):
        create_customer(store, [])


def test_same_digit_calls_and_webhooks_are_tenant_isolated(tmp_path, monkeypatch):
    client = app_client(tmp_path)
    store = client.application.extensions["settings_store"]
    service = client.application.extensions["telephony_service"]
    tenants = [customer(store, name) for name in ("first", "second")]
    calls = []
    for owner, number in zip(tenants, ("+13025550041", "+13025550042")):
        store.save_number({"number": number, "provider": "TestProvider", "owner_user_id": owner, "active": True})
        store.add_extension_to_number(number, owner, {"extension": "101"})
        call = Call(call_id=f"tenant-{owner}", contact_id=None, member_id=None, extension=f"101@{number}", phone="+13025550100")
        service.store.create(call)
        calls.append(call)
    _, token = store.create_api_key("first key", "calls:read,calls:write,webhooks:manage", tenants[0])
    headers = {"Authorization": "Bearer " + token}
    events = []
    monkeypatch.setattr(service, "notify_crm", lambda *a: events.append(a))
    assert client.get(f"/api/v1/calls/{calls[0].call_id}", headers=headers).status_code == 200
    assert client.get(f"/api/v1/calls/{calls[1].call_id}", headers=headers).status_code == 404
    listed = client.get("/api/v1/calls", headers=headers).json
    assert [c["call_id"] for c in listed["calls"]] == [calls[0].call_id]
    denied = client.post(f"/api/v1/calls/{calls[1].call_id}/disposition", headers=headers, json={"notes": "injected", "disposition": "wrong"})
    assert denied.status_code == 404 and "call" not in denied.json
    unchanged = service.store.get(calls[1].call_id)
    assert unchanged.notes is None and unchanged.disposition is None and not events
    assert client.post(f"/api/v1/calls/{calls[0].call_id}/disposition", headers=headers, json={"notes": "own"}).status_code == 200
    hook = store.save_webhook({"name": "second hook", "url": "https://crm.example/events"}, tenants[1])
    attempts = []
    monkeypatch.setattr(service, "test_webhook", lambda *a: attempts.append(a))
    assert client.post(f"/api/v1/webhooks/{hook}/test", headers=headers).status_code == 404
    assert not attempts
    # Extension reassignment must not transfer historical calls.
    with store._connect() as db:
        db.execute("UPDATE extensions SET owner_user_id=? WHERE owner_user_id=?", (tenants[1], tenants[0]))
    assert client.get(f"/api/v1/calls/{calls[0].call_id}", headers=headers).status_code == 200
    assert client.get("/api/v1/calls", headers=headers).json["total"] == 1


def test_ambiguous_legacy_calls_fail_closed(tmp_path):
    client = app_client(tmp_path)
    store = client.application.extensions["settings_store"]
    service = client.application.extensions["telephony_service"]
    for i, name in enumerate(("first", "second")):
        owner = customer(store, name)
        number = f"+1302555004{i}"
        store.save_number({"number": number, "provider": "TestProvider", "owner_user_id": owner, "active": True})
        store.add_extension_to_number(number, owner, {"extension": "101"})
    # Simulate a pre-upgrade row, which has no ownership snapshot.
    service.store.create(Call(call_id="ambiguous", contact_id=None, member_id=None, extension="101", phone="+13025550100"))
    with service.store._connect() as db:
        db.execute("UPDATE calls SET owner_user_id=NULL WHERE call_id='ambiguous'")
    restarted = TelephonyService(service.asterisk, service.config, settings_store=store)
    assert restarted.store.get("ambiguous").owner_user_id == -1


def test_idempotency_fingerprint_and_crash_recovery(tmp_path, monkeypatch):
    database = Database(f"sqlite:///{tmp_path}/requests.db")
    now = [1000]
    monkeypatch.setattr("app.idempotency.time.time", lambda: now[0])
    first, call_id = reserve(database, "client", "request", {"phone": "one"})
    assert first
    assert reserve(database, "client", "request", {"phone": "one"}) == (False, call_id)
    with pytest.raises(ValueError, match="different input"):
        reserve(database, "client", "request", {"phone": "two"})
    now[0] += 61
    assert reserve(database, "client", "request", {"phone": "one"}) == (True, call_id)
    # A late owner cannot insert a second call/originate under the reservation.
    calls = CallStore(str(tmp_path / "calls.db"))
    calls.create(Call(call_id=call_id, contact_id=None, member_id=None, extension="101", phone="one"))
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        calls.create(Call(call_id=call_id, contact_id=None, member_id=None, extension="101", phone="one"))


def test_idempotency_replays_call_after_finalize_crash(tmp_path):
    client = app_client(tmp_path)
    headers = {"Authorization": "Bearer test-token", "Idempotency-Key": "crash-recovery-123"}
    data = {"phone": "+13025550100", "extension": "101"}
    store = client.application.extensions["settings_store"]
    _, call_id = reserve(store.database, "legacy-master", headers["Idempotency-Key"], data)
    service = client.application.extensions["telephony_service"]
    service.store.create(Call(call_id=call_id, contact_id=None, member_id=None, extension="101", phone=data["phone"]))
    response = client.post("/api/v1/calls", headers=headers, json=data)
    assert response.status_code == 200 and response.json["idempotent_replay"]
    assert client.post("/api/v1/calls", headers=headers, json={**data, "phone": "+13025550101"}).status_code == 409


def test_limiter_is_shared_atomic_and_expires(tmp_path, monkeypatch):
    database = Database(f"sqlite:///{tmp_path}/limits.db")
    limiter = RateLimiter(database)
    other = RateLimiter(Database(database.uri))
    now = [1200]
    monkeypatch.setattr("app.rate_limits.time.time", lambda: now[0])
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda i: (limiter if i % 2 else other).limited("shared", 10), range(40)))
    assert outcomes.count(False) == 10
    assert not other.limited("different", 10)
    now[0] += 60
    assert not other.limited("shared", 10)
    now[0] += 86460
    assert not limiter.limited("new", 10)
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] == 1


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://[::1]/", "http://169.254.169.254/latest", "http://10.0.0.1/", "http://localhost/", "http://user:pass@example.com/", "file:///etc/passwd", "https://example.com:99999/"])
def test_webhook_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        webhook_transport.validate_url(url)


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::ffff:127.0.0.1"]])
def test_webhook_blocks_private_and_mixed_dns(monkeypatch, addresses):
    monkeypatch.setattr(webhook_transport.socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", (ip, 443)) for ip in addresses])
    with pytest.raises(ValueError):
        webhook_transport.post("https://customer.example/hook")


def test_webhook_pins_dns_disables_proxy_and_redirects(monkeypatch):
    lookups, captured = [], {}
    def lookup(*a, **kw):
        lookups.append(a)
        return [(2, 1, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(webhook_transport.socket, "getaddrinfo", lookup)
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def mount(self, prefix, adapter):
            captured["pool"] = adapter.pinned_pool
        def post(self, url, **kw):
            captured.update(kw)
            assert not self.trust_env
            return SimpleNamespace(status_code=302)
    monkeypatch.setattr(webhook_transport.requests, "Session", Session)
    webhook_transport.post("https://customer.example/hook", headers={"X-Test": "yes"})
    assert len(lookups) == 1
    assert captured["pool"].host == "93.184.216.34"
    assert captured["pool"].conn_kw["server_hostname"] == "customer.example"
    assert captured["pool"].assert_hostname == "customer.example"
    assert captured["headers"]["Host"] == "customer.example"
    assert captured["allow_redirects"] is False and captured["stream"] is True


def test_ivr_clock_wakes_independently_of_blocked_maintenance():
    from app.ari_worker import _ivr_clock
    stopped, wake, tick = threading.Event(), threading.Event(), threading.Event()
    service = SimpleNamespace(process_ivr_timeouts=tick.set)
    logger = SimpleNamespace(exception=lambda *a: None)
    worker = threading.Thread(target=_ivr_clock, args=(service, logger, stopped, wake, threading.RLock()))
    worker.start()
    try:
        wake.set()  # An ARI session arrived while the maintenance loop is idle/busy.
        assert tick.wait(0.5)
    finally:
        stopped.set()
        wake.set()
        worker.join(2)
    assert not worker.is_alive()


def test_signup_and_login_reject_non_objects(tmp_path):
    client = app_client(tmp_path)
    assert client.post("/signup", json=["bad"]).status_code == 400
    assert client.post("/login", json=["bad"]).status_code == 400


def test_upgrade_honors_old_idempotency_reservations(tmp_path):
    client = app_client(tmp_path)
    database = client.application.extensions["settings_store"].database
    with database.connect() as db:
        db.execute("INSERT INTO api_idempotency(client,request_key,call_id) VALUES('client','finished','old-call')")
        db.execute("INSERT INTO api_idempotency(client,request_key) VALUES('client','pending')")
    assert reserve(database, "client", "finished", {"phone": "one"}) == (True, "old-call")
    with pytest.raises(ValueError, match="reconciliation"):
        reserve(database, "client", "pending", {"phone": "one"})


def test_concurrent_reservations_have_one_lease_owner(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/requests.db")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: reserve(database, "client", "key", {"phone": "one"}), range(8)))
    assert sum(int(claimed) for claimed, _ in results) == 1
    assert len({call_id for _, call_id in results}) == 1


@pytest.mark.parametrize("hops,expected", [(0, "127.0.0.1"), (1, "198.51.100.22")])
def test_forwarded_addresses_only_trusted_when_opted_in(tmp_path, monkeypatch, hops, expected):
    from app import create_app
    base = app_client(tmp_path)
    class ProxyConfig(base.application.extensions["telephony_service"].config):
        TRUSTED_PROXY_HOPS = hops
    app = create_app(ProxyConfig)
    keys = []
    monkeypatch.setattr(app.extensions["rate_limiter"], "limited", lambda key, *a: keys.append(key) or False)
    response = app.test_client().post("/login", json={"username": "unknown", "password": "bad"}, headers={"X-Forwarded-For": "198.51.100.22"})
    assert response.status_code == 401
    assert "login:ip:" + expected in keys


def test_authenticated_clients_do_not_share_proxy_ip_budget(tmp_path, monkeypatch):
    import time
    now = int(time.time())
    monkeypatch.setattr("app.rate_limits.time.time", lambda: now)
    client = app_client(tmp_path)
    store = client.application.extensions["settings_store"]
    headers = []
    for name in ("first", "second"):
        owner = customer(store, name)
        _, token = store.create_api_key(name + " key", "calls:read", owner)
        headers.append({"Authorization": "Bearer " + token})
    for _ in range(120):
        assert client.get("/api/v1/calls", headers=headers[0]).status_code == 200
    assert client.get("/api/v1/calls", headers=headers[0]).status_code == 429
    assert client.get("/api/v1/calls", headers=headers[1]).status_code == 200


def test_redirect_is_delivery_failure(monkeypatch):
    response = SimpleNamespace(status_code=302, close=lambda: None)
    monkeypatch.setattr(webhook_transport, "post", lambda *a, **kw: response)
    assert TelephonyService._send_webhook("https://example.com/hook", "", {}) == (False, 302, "HTTP 302")
