"""MySQL schema upgrade of a database created before per-number extensions.

These tests run the MySQL code paths against a recording connection, so they
need no server. They pin the three things that broke an existing install:

* the identity migration must run before the sip-username normalisation, which
  reads `extensions.id` and `extensions.phone_number_id` (the startup crash);
* the legacy `extensions` table (primary key `extension`, VARCHAR(3)) must be
  turned into the identity table, and a current table must be left alone;
* missing columns are added and too-narrow VARCHARs are widened, never narrowed.

They do not prove that MySQL accepts each statement; that needs a MySQL server.
"""
from contextlib import contextmanager

from sqlalchemy import BigInteger, String
from sqlalchemy.types import VARCHAR

import app.admin as admin
from app.admin import SettingsStore
from app.database import metadata, reconcile_statements


# The `extensions` table exactly as the MySQL install reported it (DESCRIBE).
LEGACY_EXTENSIONS = {
    "extension": {"type": VARCHAR(3)},
    "display_name": {"type": VARCHAR(120)},
    "sip_username": {"type": VARCHAR(80)},
    "sip_password_enc": {"type": VARCHAR(2048)},
    "webrtc_enabled": {"type": BigInteger()},
    "recording_enabled": {"type": BigInteger()},
    "voicemail_enabled": {"type": BigInteger()},
    "voicemail_pin_enc": {"type": VARCHAR(2048)},
    "voicemail_email": {"type": VARCHAR(254)},
    "active": {"type": BigInteger()},
    "owner_user_id": {"type": BigInteger()},
    "created_at": {"type": BigInteger()},
    "updated_at": {"type": BigInteger()},
}


def current_shape():
    """The live shape a current install has: every metadata column, at its own type."""
    return {
        table.name: {column.name: {"type": column.type} for column in table.columns}
        for table in metadata.sorted_tables
    }


class RecordingConnection:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.sink.append(str(sql))

        class Result:
            def fetchone(self):
                return None

            def fetchall(self):
                return []

        return Result()


def store_with(events):
    """A SettingsStore wired to a MySQL database object, with no server behind it."""
    store = SettingsStore.__new__(SettingsStore)
    store.secret_key = b"x" * 40

    class FakeDatabase:
        is_mysql = True
        MIGRATION_LOCK = "eip_telephony.schema_migrate"

        def create_all(self):
            events.append("create_all")

        def columns(self, table):
            return {"name", "id", "phone_number_id", "full_name", "company_name", "job_role", "phone"}

        @contextmanager
        def advisory_lock(self, name):
            events.append(f"lock:{name}")
            yield
            events.append(f"unlock:{name}")

        engine = None

    store.database = FakeDatabase()
    store._connect = lambda: RecordingConnection(events)
    return store


def test_reconcile_upgrades_the_legacy_extensions_table():
    statements = reconcile_statements({"extensions": LEGACY_EXTENSIONS})

    assert "ALTER TABLE extensions ADD COLUMN phone_number_id BIGINT" in statements
    assert any(s.startswith("ALTER TABLE extensions MODIFY COLUMN extension VARCHAR(64) NOT NULL") for s in statements)
    # The primary key is planned by the identity migration, never by reconcile.
    assert not any(" id " in s or s.endswith(" id") for s in statements)
    assert not any("MODIFY COLUMN sip_username" in s for s in statements)


def test_reconcile_widens_the_number_scoped_extension_columns():
    existing = {
        "phone_numbers": {"inbound_extension": {"type": VARCHAR(3)}},
        "admin_users": {"extension": {"type": VARCHAR(3)}},
    }
    statements = reconcile_statements(existing)
    assert any(s.startswith("ALTER TABLE phone_numbers MODIFY COLUMN inbound_extension VARCHAR(64)") for s in statements)
    assert any(s.startswith("ALTER TABLE admin_users MODIFY COLUMN extension VARCHAR(64)") for s in statements)


def test_reconcile_is_a_no_op_on_a_current_schema():
    assert reconcile_statements(current_shape()) == []


def test_reconcile_never_narrows_a_column():
    wider = {"extensions": {"extension": {"type": VARCHAR(200)}}}
    assert not any("MODIFY" in s for s in reconcile_statements(wider))


def test_identity_ddl_for_the_legacy_table(monkeypatch):
    class Inspector:
        def get_columns(self, table):
            return [{"name": name} for name in LEGACY_EXTENSIONS]

        def get_pk_constraint(self, table):
            return {"constrained_columns": ["extension"]}

        def get_indexes(self, table):
            return []

    monkeypatch.setattr(admin, "inspect", lambda engine: Inspector())
    events = []
    store = store_with(events)
    store._ensure_extension_identity_schema()

    assert events == [
        "ALTER TABLE extensions DROP PRIMARY KEY",
        "ALTER TABLE extensions ADD COLUMN id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY FIRST",
        "ALTER TABLE extensions ADD COLUMN phone_number_id BIGINT NULL",
        "CREATE UNIQUE INDEX uq_extensions_number_extension ON extensions(phone_number_id,extension)",
    ]


def test_identity_ddl_is_a_no_op_once_upgraded(monkeypatch):
    class Inspector:
        def get_columns(self, table):
            return [{"name": "id"}, {"name": "extension"}, {"name": "phone_number_id"}]

        def get_pk_constraint(self, table):
            return {"constrained_columns": ["id"]}

        def get_indexes(self, table):
            return [{"name": "uq_extensions_number_extension"}]

    monkeypatch.setattr(admin, "inspect", lambda engine: Inspector())
    events = []
    store_with(events)._ensure_extension_identity_schema()
    assert events == []


def test_mysql_startup_migrates_identity_before_it_normalises(monkeypatch):
    """The regression: normalising sip usernames used to run before the id columns existed."""
    events = []
    store = store_with(events)
    store._create_schema = lambda: events.append("schema")
    store._migrate_extension_identity = lambda: events.append("identity")
    store.normalise_sip_usernames = lambda db=None: events.append("normalise")

    store._init_db()

    assert events == [
        "schema",
        "lock:eip_telephony.schema_migrate",
        "identity",
        "normalise",
        "unlock:eip_telephony.schema_migrate",
    ]


def test_mysql_schema_step_no_longer_normalises_before_the_identity_columns(monkeypatch):
    """The MySQL schema step itself must not read the new columns; it runs before they exist."""
    events = []
    store = store_with(events)
    store.normalise_sip_usernames = lambda db=None: events.append("normalise")
    store._create_schema()

    assert "normalise" not in events
    assert "create_all" in events
