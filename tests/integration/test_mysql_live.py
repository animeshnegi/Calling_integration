"""Real MySQL acceptance tests. Every test uses a disposable, random database.

TEST_MYSQL_URI must name a dedicated test server/user with CREATE/DROP DATABASE
permission. Never point it at production; the URI's database itself is not used.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.admin import SettingsStore
from app.database import Database
from app.idempotency import reserve
from app.models import Call, CallStore
from app.rate_limits import RateLimiter

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_MYSQL_URI"), reason="set TEST_MYSQL_URI for a dedicated real-MySQL integration server")


@pytest.fixture
def mysql_uri():
    url = make_url(os.environ["TEST_MYSQL_URI"]).set(drivername="mysql+pymysql", database=None)
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    name = "eip_test_" + uuid.uuid4().hex
    with admin.connect() as db:
        db.execute(text(f"CREATE DATABASE {name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    try:
        yield url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as db:
            db.execute(text(f"DROP DATABASE {name}"))
        admin.dispose()


def test_fresh_schema_and_representative_crud(mysql_uri):
    store = SettingsStore(mysql_uri, "integration-secret-key-1234567890")
    calls = CallStore(mysql_uri)
    try:
        owner = store.save_user({"username": "customer", "email": "customer@example.com", "password": "customer-password-123", "role": "user"})
        _, token = store.create_api_key("CRM", "calls:read", owner)
        assert store.authenticate_api_key(token, "calls:read")["owner_user_id"] == owner
        calls.create(Call(call_id="mysql-call", contact_id=None, member_id=None, extension="101@+13025550100", phone="+13025550101", owner_user_id=owner))
        calls.update("mysql-call", disposition="follow_up", notes="Live MySQL")
        found, total = calls.search(owner_user_id=owner)
        assert total == 1 and found[0].notes == "Live MySQL"
        assert calls.summary(owner_user_id=owner)["total"] == 1
        hook = store.save_webhook({"name": "CRM", "url": "https://crm.example/events"}, owner)
        delivery = store.enqueue_webhook(hook, "call.started", {"event": "call.started"})
        assert store.pending_webhook_deliveries()[0]["id"] == delivery
        store.finish_webhook_delivery(delivery, False, "test failure")
        assert store.list_webhook_deliveries()[0]["attempts"] == 1
        limiter = RateLimiter(store.database)
        assert not limiter.limited("live", 1)
        assert limiter.limited("live", 1)
        assert reserve(store.database, "client", "key", {"phone": "one"})[0]
        assert not reserve(store.database, "client", "key", {"phone": "one"})[0]
    finally:
        calls.database.engine.dispose()
        store.database.engine.dispose()


def test_concurrent_schema_startup(mysql_uri):
    def startup(_):
        store = SettingsStore(mysql_uri, "integration-secret-key-1234567890")
        try:
            assert "id" in store.database.columns("extensions")
        finally:
            store.database.engine.dispose()
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(startup, range(3)))


def test_legacy_extension_and_call_schema_upgrade(mysql_uri):
    engine = create_engine(mysql_uri)
    with engine.begin() as db:
        db.execute(text("""CREATE TABLE extensions (
            extension VARCHAR(3) PRIMARY KEY,
            display_name VARCHAR(120) NOT NULL DEFAULT '',
            sip_username VARCHAR(80) NOT NULL,
            sip_password_enc TEXT NOT NULL,
            webrtc_enabled INTEGER NOT NULL DEFAULT 0,
            recording_enabled INTEGER NOT NULL DEFAULT 0,
            voicemail_enabled INTEGER NOT NULL DEFAULT 0,
            voicemail_pin_enc VARCHAR(2048) NOT NULL DEFAULT '',
            voicemail_email VARCHAR(254) NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            owner_user_id BIGINT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""))
        db.execute(text("INSERT INTO extensions(extension,sip_username,sip_password_enc) VALUES('101','101','')"))
    store = SettingsStore(mysql_uri, "integration-secret-key-1234567890")
    try:
        assert {"id", "phone_number_id"} <= store.database.columns("extensions")
        assert store.list_extensions()[0]["extension"] == "101"
        with engine.begin() as db:
            db.execute(text("ALTER TABLE calls DROP COLUMN owner_user_id"))
        calls = CallStore(mysql_uri)
        try:
            assert "owner_user_id" in calls.database.columns("calls")
        finally:
            calls.database.engine.dispose()
    finally:
        store.database.engine.dispose()
        engine.dispose()
