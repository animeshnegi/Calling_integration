from pathlib import Path

from sqlalchemy import create_mock_engine

from app.database import Database, MySQLConnection, metadata


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
