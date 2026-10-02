import datetime
import re
from pathlib import Path

from sqlalchemy import create_mock_engine

from app.database import Database, DBRow, MySQLConnection, _make_row, metadata, timestamp_column_fixes


ROOT = Path(__file__).resolve().parents[1]


def test_mysql_schema_compiles_all_application_tables():
    statements = []
    engine = create_mock_engine("mysql+pymysql://", lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=engine.dialect))))
    metadata.create_all(engine)
    expected = {
        "admin_users", "settings", "extensions", "phone_numbers", "sip_providers",
        "webhook_endpoints", "webhook_deliveries", "api_keys", "api_idempotency",
        "email_config", "voicemail_deliveries", "billing_invoices", "calls",
        "customer_requests", "customer_sip_accounts", "call_routes", "activity_history", "notifications",
        "extension_groups", "routing_flows",
    }
    assert expected <= set(metadata.tables)
    rendered = "\n".join(statements)
    assert "CREATE TABLE calls" in rendered
    assert "CREATE TABLE admin_users" in rendered


def test_mysql_timestamp_defaults_only_appear_on_datetime_columns():
    """MySQL error 1067: DEFAULT CURRENT_TIMESTAMP is invalid on VARCHAR/TEXT.

    Every column using a CURRENT_TIMESTAMP server default must be a real
    DATETIME column, and audit columns must keep the DDL default so the raw
    SQL INSERTs (which omit created_at/updated_at) succeed in strict mode.
    """
    statements = []
    engine = create_mock_engine("mysql+pymysql://", lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=engine.dialect))))
    metadata.create_all(engine)
    rendered = "\n".join(statements)
    for line in rendered.splitlines():
        if "CURRENT_TIMESTAMP" in line:
            assert "DATETIME" in line, f"non-DATETIME column uses CURRENT_TIMESTAMP default: {line.strip()}"
    admin_users_ddl = next(stmt for stmt in statements if "CREATE TABLE admin_users" in stmt)
    assert re.search(r"created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP", admin_users_ddl)
    assert re.search(r"updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP", admin_users_ddl)


def test_mysql_text_and_blob_columns_never_carry_defaults():
    """MySQL error 1101: TEXT/BLOB/JSON columns cannot have a DEFAULT clause.

    SQLAlchemy happily renders 'TEXT NOT NULL DEFAULT …' but the server
    rejects it at CREATE TABLE time, so guard every table's generated DDL.
    """
    statements = []
    engine = create_mock_engine("mysql+pymysql://", lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=engine.dialect))))
    metadata.create_all(engine)
    for statement in statements:
        for line in statement.splitlines():
            upper = line.upper()
            if "DEFAULT" not in upper:
                continue
            column_type = upper.split("DEFAULT")[0]
            assert not any(
                banned in column_type
                for banned in (" TEXT", " BLOB", " TINYTEXT", " MEDIUMTEXT", " LONGTEXT", " JSON")
            ), f"TEXT/BLOB column with DEFAULT is invalid in MySQL: {line.strip()}"


def test_timestamp_fixes_repair_varchar_columns_created_by_older_images():
    existing = {
        "admin_users": {
            "created_at": {"type": "VARCHAR(40)", "default": None, "nullable": False},
            "updated_at": {"type": "VARCHAR(40)", "default": None, "nullable": False},
            "username": {"type": "VARCHAR(80)", "default": None, "nullable": False},
        },
        "webhook_deliveries": {
            "delivered_at": {"type": "VARCHAR(40)", "default": None, "nullable": True},
        },
    }
    statements = timestamp_column_fixes(existing)
    assert "UPDATE admin_users SET created_at=CURRENT_TIMESTAMP WHERE created_at=''" in statements
    assert "ALTER TABLE admin_users MODIFY created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP" in statements
    assert "ALTER TABLE admin_users MODIFY updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP" in statements
    assert "UPDATE webhook_deliveries SET delivered_at=NULL WHERE delivered_at=''" in statements
    assert "ALTER TABLE webhook_deliveries MODIFY delivered_at DATETIME" in statements
    assert not any("username" in statement for statement in statements)


def test_timestamp_fixes_are_noop_for_correct_schema():
    existing = {
        "admin_users": {
            "created_at": {"type": "DATETIME", "default": "CURRENT_TIMESTAMP", "nullable": False},
            "updated_at": {"type": "DATETIME", "default": "CURRENT_TIMESTAMP", "nullable": False},
        },
        "notifications": {
            "read_at": {"type": "DATETIME", "default": None, "nullable": True},
        },
    }
    assert timestamp_column_fixes(existing) == []


def test_timestamp_fixes_restore_missing_server_default_without_rewriting_data():
    existing = {
        "admin_users": {
            "created_at": {"type": "DATETIME", "default": None, "nullable": False},
        },
    }
    statements = timestamp_column_fixes(existing)
    assert statements == ["ALTER TABLE admin_users MODIFY created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"]


def test_cursor_rows_convert_mysql_datetimes_to_iso_strings():
    row = _make_row({
        "created_at": datetime.datetime(2026, 10, 2, 18, 5, 36),
        "due_at": datetime.date(2026, 10, 9),
        "username": "admin",
        "attempts": 3,
        "last_error": None,
    }.items())
    assert isinstance(row, DBRow)
    assert row["created_at"] == "2026-10-02 18:05:36"
    assert row["due_at"] == "2026-10-09"
    assert row["username"] == "admin"
    assert row["attempts"] == 3
    assert row["last_error"] is None


def test_existing_pymysql_uri_is_not_rewritten_or_duplicated():
    database = Database("mysql+pymysql://root:@host.docker.internal:3306/eip_telephony?charset=utf8mb4")
    assert database.engine.url.drivername == "mysql+pymysql"
    assert database.engine.url.host == "host.docker.internal"
    assert database.engine.url.username == "root"
    assert database.engine.url.password == ""
    assert database.engine.url.database == "eip_telephony"
    assert database.engine.url.query["charset"] == "utf8mb4"
    assert "mysql+pymysql+pymysql" not in database.engine.url.render_as_string(hide_password=False)


def test_plain_mysql_uri_selects_pymysql_without_changing_components():
    database = Database("mysql://eip_user:p%40ss@db.internal:3307/eip?charset=utf8mb4")
    assert database.engine.url.drivername == "mysql+pymysql"
    assert database.engine.url.password == "p@ss"
    assert database.engine.url.port == 3307


def test_mysql_query_translation_covers_sqlite_compatibility_syntax():
    translated = MySQLConnection._translate(
        "INSERT OR IGNORE INTO settings(`key`,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE created_at<datetime('now','-24 hours')"
    )
    assert "INSERT IGNORE" in translated
    assert "ON DUPLICATE KEY UPDATE" in translated
    assert "value=VALUES(value)" in translated
    assert translated.count("%s") == 2
    assert "INTERVAL 24 HOUR" in translated


def test_compose_requires_database_uri_and_env_documents_mysql():
    compose = (ROOT / "docker-compose.yml").read_text()
    env = (ROOT / ".env.example").read_text()
    assert compose.count("DATABASE_URI: ${DATABASE_URI:?") == 2
    assert "DATABASE_URI=mysql+pymysql://" in env
    assert "charset=utf8mb4" in env


def test_compose_caps_python_service_resources():
    """A crash-restart loop must never be able to saturate the host again."""
    compose = (ROOT / "docker-compose.yml").read_text()
    assert compose.count("cpus:") >= 2
    assert compose.count("memory:") >= 2


def test_boot_failures_back_off_instead_of_hot_looping():
    wsgi = (ROOT / "wsgi.py").read_text()
    worker = (ROOT / "app" / "ari_worker.py").read_text()
    assert "time.sleep(10)" in wsgi and "raise" in wsgi
    assert "time.sleep(10)" in worker
