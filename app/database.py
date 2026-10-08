"""Database compatibility layer and automatic MySQL schema creation."""

from __future__ import annotations

import datetime as _datetime
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, MetaData, String, Table, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError


metadata = MetaData()

# MySQL only allows DEFAULT CURRENT_TIMESTAMP on TIMESTAMP/DATETIME columns, so
# every database-managed timestamp column is a real DATETIME with a server-side
# default. The CursorAdapter converts fetched datetime values back to ISO
# strings so callers see the same text values the SQLite backend produces.
def _timestamps():
    return (
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )


admin_users = Table("admin_users", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("username", String(80), unique=True, nullable=False),
    Column("email", String(254), nullable=False, server_default=""), Column("extension", String(64), nullable=False, server_default=""),
    Column("full_name", String(120), nullable=False, server_default=""), Column("company_name", String(160), nullable=False, server_default=""),
    Column("job_role", String(120), nullable=False, server_default=""), Column("phone", String(30), nullable=False, server_default=""),
    Column("password_hash", String(512), nullable=False), Column("role", String(20), nullable=False, server_default="user"),
    Column("active", Integer, nullable=False, server_default="1"), *_timestamps())
settings = Table("settings", metadata, Column("key", String(100), primary_key=True), Column("value", Text, nullable=False), Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")))
extensions = Table("extensions", metadata,
    # An extension is identified by the phone number it belongs to and its
    # digits: `101` on +13025550001 and `101` on +13025550002 are two different
    # desks. `id` is the stable internal handle an extension keeps for its whole
    # life, including while it is moved between numbers. `phone_number_id` is
    # NULL for a platform row (the operator's own phones, which have no customer
    # line) - a customer row always names one of that customer's numbers, and the
    # migration places every legacy row on the number it belongs to.
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("extension", String(64), nullable=False, server_default=""),
    Column("phone_number_id", BigInteger), Column("display_name", String(120), nullable=False, server_default=""),
    Column("sip_username", String(80), nullable=False), Column("sip_password_enc", Text, nullable=False),
    Column("webrtc_enabled", Integer, nullable=False, server_default="0"), Column("recording_enabled", Integer, nullable=False, server_default="0"),
    Column("voicemail_enabled", Integer, nullable=False, server_default="0"), Column("voicemail_pin_enc", String(2048), nullable=False, server_default=""),
    Column("voicemail_email", String(254), nullable=False, server_default=""), Column("active", Integer, nullable=False, server_default="1"),
    Column("owner_user_id", BigInteger), *_timestamps(),
    # One `101` per phone number. Two numbers of the same customer - and two
    # different customers - may each have their own 101; the same number may not
    # hold the same digits twice.
    UniqueConstraint("phone_number_id", "extension", name="uq_extensions_number_extension"))
phone_numbers = Table("phone_numbers", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("number", String(16), unique=True, nullable=False),
    Column("provider", String(80), nullable=False, server_default=""), Column("description", String(160), nullable=False, server_default=""),
    Column("inbound_extension", String(64), nullable=False, server_default=""), Column("default_outbound", Integer, nullable=False, server_default="0"),
    Column("active", Integer, nullable=False, server_default="1"), Column("owner_user_id", BigInteger),
    Column("monthly_price_cents", Integer, nullable=False, server_default="500"), Column("billing_start", String(10), nullable=False, server_default=""),
    Column("billing_cycle_day", Integer, nullable=False, server_default="1"), Column("discontinue_at", String(10), nullable=False, server_default=""), *_timestamps())
sip_providers = Table("sip_providers", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("name", String(80), unique=True, nullable=False),
    Column("server", String(255), nullable=False), Column("port", Integer, nullable=False, server_default="5060"),
    Column("username", String(255), nullable=False, server_default=""), Column("password_enc", Text, nullable=False),
    Column("transport", String(10), nullable=False, server_default="udp"), Column("codecs", String(255), nullable=False, server_default="ulaw,alaw"),
    Column("allowed_ips", Text, nullable=False), Column("active", Integer, nullable=False, server_default="1"), *_timestamps())
webhook_endpoints = Table("webhook_endpoints", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("name", String(80), unique=True, nullable=False),
    Column("url", String(1000), nullable=False), Column("token_enc", Text, nullable=False), Column("events", String(1000), nullable=False, server_default="*"),
    Column("active", Integer, nullable=False, server_default="1"), Column("owner_user_id", BigInteger), *_timestamps())
webhook_deliveries = Table("webhook_deliveries", metadata,
    Column("id", String(36), primary_key=True), Column("endpoint_id", BigInteger, nullable=False), Column("event", String(80), nullable=False),
    Column("payload", Text, nullable=False), Column("status", String(20), nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"), Column("last_error", Text),
    Column("next_attempt_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")), Column("delivered_at", DateTime), *_timestamps())
api_keys = Table("api_keys", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("name", String(80), unique=True, nullable=False),
    Column("prefix", String(20), nullable=False), Column("key_hash", String(64), unique=True, nullable=False),
    Column("scopes", String(500), nullable=False, server_default="*"), Column("active", Integer, nullable=False, server_default="1"),
    Column("owner_user_id", BigInteger), Column("last_used_at", DateTime), Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")))
api_idempotency = Table("api_idempotency", metadata,
    Column("client", String(100), primary_key=True), Column("request_key", String(128), primary_key=True),
    Column("call_id", String(128)), Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")))
email_config = Table("email_config", metadata, Column("id", Integer, primary_key=True), Column("sendgrid_api_key_enc", Text, nullable=False),
    Column("from_email", String(254), nullable=False, server_default=""), Column("from_name", String(120), nullable=False, server_default="EIP Telephony Voicemail"),
    Column("enabled", Integer, nullable=False, server_default="0"), Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")))
voicemail_deliveries = Table("voicemail_deliveries", metadata,
    Column("fingerprint", String(128), primary_key=True), Column("mailbox", String(64), nullable=False), Column("recipient", String(254), nullable=False),
    Column("status", String(20), nullable=False, server_default="pending"), Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text), Column("delivered_at", DateTime), Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")))
billing_invoices = Table("billing_invoices", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("user_id", BigInteger, nullable=False), Column("number", String(16), nullable=False),
    Column("period_start", String(10), nullable=False), Column("period_end", String(10), nullable=False), Column("amount_cents", Integer, nullable=False),
    Column("status", String(20), nullable=False, server_default="open"), Column("due_at", String(10), nullable=False), Column("paid_at", DateTime),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")))
customer_requests = Table("customer_requests", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("user_id", BigInteger, nullable=False),
    Column("request_type", String(40), nullable=False, server_default="number"), Column("details", Text, nullable=False),
    Column("status", String(20), nullable=False, server_default="pending"), Column("admin_note", Text),
    Column("resolved_at", DateTime), *_timestamps())
customer_sip_accounts = Table("customer_sip_accounts", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("owner_user_id", BigInteger, nullable=False),
    Column("label", String(120), nullable=False), Column("sip_username", String(100), unique=True, nullable=False),
    Column("sip_password_enc", Text, nullable=False), Column("server", String(255), nullable=False),
    Column("port", Integer, nullable=False, server_default="5060"), Column("transport", String(10), nullable=False, server_default="udp"),
    Column("phone_number", String(16), nullable=False, server_default=""), Column("extension", String(64), nullable=False, server_default=""),
    Column("registration_status", String(20), nullable=False, server_default="offline"), Column("last_registered_at", DateTime),
    Column("active", Integer, nullable=False, server_default="1"), *_timestamps())
call_routes = Table("call_routes", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("owner_user_id", BigInteger, nullable=False),
    Column("phone_number", String(16), unique=True, nullable=False), Column("name", String(120), nullable=False, server_default="Main call flow"),
    Column("route_json", Text, nullable=False), Column("active", Integer, nullable=False, server_default="1"), *_timestamps())
extension_groups = Table("extension_groups", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("owner_user_id", BigInteger, nullable=False),
    Column("name", String(80), nullable=False), Column("members", Text, nullable=False),
    Column("timeout", Integer, nullable=False, server_default="25"), Column("active", Integer, nullable=False, server_default="1"),
    *_timestamps())
routing_flows = Table("routing_flows", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("owner_user_id", BigInteger, nullable=False),
    Column("target_type", String(20), nullable=False, server_default="extension"), Column("target", String(64), nullable=False),
    Column("name", String(120), nullable=False, server_default="Call flow"), Column("route_json", Text, nullable=False),
    Column("active", Integer, nullable=False, server_default="1"), *_timestamps())
activity_history = Table("activity_history", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("owner_user_id", BigInteger),
    Column("actor_user_id", BigInteger), Column("action", String(80), nullable=False), Column("resource_type", String(40), nullable=False),
    Column("resource_id", String(128)), Column("description", String(500), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")))
notifications = Table("notifications", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("user_id", BigInteger, nullable=False),
    Column("kind", String(40), nullable=False), Column("title", String(160), nullable=False), Column("message", String(1000), nullable=False),
    Column("read_at", DateTime), Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")))
calls = Table("calls", metadata,
    Column("call_id", String(128), primary_key=True), Column("contact_id", String(255)), Column("member_id", String(255)),
    Column("extension", String(64), nullable=False), Column("phone", String(16), nullable=False), Column("caller_id_number", String(16)),
    Column("provider", String(80)), Column("direction", String(20), nullable=False), Column("status", String(40), nullable=False),
    Column("answered", Integer, nullable=False, server_default="0"), Column("started_at", String(40), nullable=False),
    Column("answered_at", String(40)), Column("ended_at", String(40)), Column("duration_seconds", Integer, nullable=False, server_default="0"),
        # MySQL error 1101: TEXT/BLOB columns cannot carry a DEFAULT clause. Every
    # insert supplies employee_channel_ids explicitly (models._COLUMNS), so no
    # server-side default is needed.
    Column("employee_channel_id", String(255)), Column("employee_channel_ids", Text, nullable=False),
    Column("customer_channel_id", String(255)), Column("bridge_id", String(255)),
    Column("recording_name", String(255)), Column("recording_format", String(20)), Column("recording_status", String(40)),
    Column("recording_path", Text), Column("disposition", String(80)), Column("notes", Text))
Index("idx_calls_employee_channel", calls.c.employee_channel_id)
Index("idx_calls_customer_channel", calls.c.customer_channel_id)
Index("idx_calls_recording_name", calls.c.recording_name)
Index("idx_calls_started_at", calls.c.started_at)


def timestamp_column_fixes(existing_columns: dict[str, dict[str, dict]]) -> list[str]:
    """Return SQL statements that repair timestamp columns from older releases.

    Earlier images created the DB-managed timestamp columns as VARCHAR (either
    with an invalid CURRENT_TIMESTAMP default or with no default at all). For
    every column the metadata declares as DATETIME, emit ALTER statements that
    convert the live column to DATETIME and restore the server-side default,
    so existing databases self-heal on startup without manual migration.

    ``existing_columns`` maps table name -> column name -> inspector info
    (at least ``type``, ``default`` and ``nullable``).
    """
    statements: list[str] = []
    for table in metadata.sorted_tables:
        table_info = existing_columns.get(table.name)
        if not table_info:
            continue
        for column in table.columns:
            if not isinstance(column.type, DateTime):
                continue
            info = table_info.get(column.name)
            if info is None:
                continue
            current_type = str(info.get("type", "")).upper()
            is_datetime = "DATETIME" in current_type or "TIMESTAMP" in current_type
            needs_default = column.server_default is not None
            has_default = info.get("default") is not None
            if is_datetime and (not needs_default or has_default):
                continue
            if not is_datetime:
                # Clear values MySQL cannot cast to DATETIME before altering.
                if column.nullable:
                    statements.append(f"UPDATE {table.name} SET {column.name}=NULL WHERE {column.name}=''")
                else:
                    statements.append(f"UPDATE {table.name} SET {column.name}=CURRENT_TIMESTAMP WHERE {column.name}=''")
            ddl = f"ALTER TABLE {table.name} MODIFY {column.name} DATETIME"
            if not column.nullable:
                ddl += " NOT NULL"
            if needs_default:
                ddl += " DEFAULT CURRENT_TIMESTAMP"
            statements.append(ddl)
    return statements


class DBRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _coerce_value(value):
    """Convert MySQL datetime/date values to the ISO strings SQLite returns."""
    if isinstance(value, _datetime.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, _datetime.date):
        return value.isoformat()
    return value


def _make_row(pairs) -> DBRow:
    return DBRow((name, _coerce_value(value)) for name, value in pairs)


class CursorAdapter:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.lastrowid

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return _make_row(row.items())
        names = [item[0] for item in self.cursor.description or ()]
        return _make_row(zip(names, row))

    def fetchall(self):
        rows = self.cursor.fetchall()
        names = [item[0] for item in self.cursor.description or ()]
        return [_make_row(row.items()) if isinstance(row, dict) else _make_row(zip(names, row)) for row in rows]


class MySQLConnection:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.raw.rollback()
        else:
            self.raw.commit()
        self.raw.close()

    @staticmethod
    def _translate(sql: str) -> str:
        sql = sql.replace("?", "%s")
        sql = sql.replace("INSERT OR IGNORE", "INSERT IGNORE")
        sql = re.sub(r"ON\s+CONFLICT\s*\([^)]*\)\s+DO\s+UPDATE\s+SET", "ON DUPLICATE KEY UPDATE", sql, flags=re.I)
        sql = re.sub(r"excluded\.([A-Za-z_][A-Za-z0-9_]*)", r"VALUES(\1)", sql, flags=re.I)
        sql = sql.replace("datetime('now','-30 days')", "(UTC_TIMESTAMP() - INTERVAL 30 DAY)")
        sql = sql.replace("datetime('now','-24 hours')", "(UTC_TIMESTAMP() - INTERVAL 24 HOUR)")
        sql = sql.replace("date('now')", "UTC_DATE()")
        sql = sql.replace("datetime('now','+' || MIN(300,30*(attempts+1)) || ' seconds')", "DATE_ADD(UTC_TIMESTAMP(), INTERVAL LEAST(300,30*(attempts+1)) SECOND)")
        return sql

    def execute(self, sql: str, params: Iterable[Any] = ()):
        cursor = self.raw.cursor()
        cursor.execute(self._translate(sql), tuple(params))
        return CursorAdapter(cursor)


class Database:
    def __init__(self, uri: str):
        self.uri = str(uri).strip()
        parsed = make_url(self.uri)
        self.is_mysql = parsed.drivername in {"mysql", "mysql+pymysql"}
        if self.is_mysql:
            # Select the PyMySQL driver explicitly. Do not perform a substring
            # replacement: an already-correct mysql+pymysql URI must remain
            # unchanged, including its percent-encoded credentials and query.
            mysql_url = parsed.set(drivername="mysql+pymysql")
            self.engine = create_engine(
                mysql_url,
                pool_pre_ping=True,
                pool_recycle=1800,
                pool_size=5,
                max_overflow=5,
                connect_args={"connect_timeout": 10},
            )
        elif parsed.drivername == "sqlite":
            path = self.uri.removeprefix("sqlite:///")
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = None
        else:
            raise ValueError("DATABASE_URI must use mysql+pymysql:// or sqlite:///")

    _SCHEMA_LOCK = "eip_telephony.schema_init"

    def create_all(self):
        if not self.is_mysql:
            return
        # Several processes boot at once (gunicorn workers + the ARI worker)
        # and each runs create_all(). checkfirst=True is not atomic, so two
        # processes can both decide a table is missing and race to CREATE it
        # (MySQL error 1050). Serialize schema setup with a server-side
        # advisory lock, and tolerate 1050 as a fallback in case the lock
        # cannot be acquired.
        with self.engine.connect() as conn:
            locked = bool(conn.execute(text("SELECT GET_LOCK(:name, 120)"), {"name": self._SCHEMA_LOCK}).scalar())
            try:
                self._create_and_repair_schema(conn)
            finally:
                if locked:
                    conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": self._SCHEMA_LOCK})

    def _create_and_repair_schema(self, conn):
        for attempt in (1, 2):
            try:
                metadata.create_all(conn, checkfirst=True)
                break
            except OperationalError as exc:
                conn.rollback()
                already_exists = getattr(exc.orig, "args", (None,))[:1] == (1050,)
                if attempt == 2 or not already_exists:
                    raise
                # Another process created the table between our existence
                # check and the CREATE; re-run so the remaining tables are
                # still created (checkfirst now sees the winner's tables).
        inspector = inspect(conn)
        existing = {
            table.name: {col["name"]: col for col in inspector.get_columns(table.name)}
            for table in metadata.sorted_tables
            if inspector.has_table(table.name)
        }
        for statement in timestamp_column_fixes(existing):
            conn.execute(text(statement))
        conn.commit()

    def connect(self):
        if self.is_mysql:
            return MySQLConnection(self.engine.raw_connection())
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def columns(self, table: str) -> set[str]:
        if self.is_mysql:
            return {column["name"] for column in inspect(self.engine).get_columns(table)}
        with self.connect() as db:
            return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
