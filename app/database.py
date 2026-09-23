"""Database compatibility layer and automatic MySQL schema creation."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import BigInteger, Column, Index, Integer, MetaData, String, Table, Text, create_engine, inspect, text
from sqlalchemy.engine import make_url


metadata = MetaData()


def _timestamps():
    return (
        Column("created_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )


admin_users = Table("admin_users", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("username", String(80), unique=True, nullable=False),
    Column("email", String(254), nullable=False, server_default=""), Column("extension", String(3), nullable=False, server_default=""),
    Column("full_name", String(120), nullable=False, server_default=""), Column("company_name", String(160), nullable=False, server_default=""),
    Column("job_role", String(120), nullable=False, server_default=""), Column("phone", String(30), nullable=False, server_default=""),
    Column("password_hash", String(512), nullable=False), Column("role", String(20), nullable=False, server_default="user"),
    Column("active", Integer, nullable=False, server_default="1"), *_timestamps())
settings = Table("settings", metadata, Column("key", String(100), primary_key=True), Column("value", Text, nullable=False), Column("updated_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
extensions = Table("extensions", metadata,
    Column("extension", String(3), primary_key=True), Column("display_name", String(120), nullable=False, server_default=""),
    Column("sip_username", String(80), nullable=False), Column("sip_password_enc", Text, nullable=False),
    Column("webrtc_enabled", Integer, nullable=False, server_default="0"), Column("recording_enabled", Integer, nullable=False, server_default="0"),
    Column("voicemail_enabled", Integer, nullable=False, server_default="0"), Column("voicemail_pin_enc", String(2048), nullable=False, server_default=""),
    Column("voicemail_email", String(254), nullable=False, server_default=""), Column("active", Integer, nullable=False, server_default="1"),
    Column("owner_user_id", BigInteger), *_timestamps())
phone_numbers = Table("phone_numbers", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("number", String(16), unique=True, nullable=False),
    Column("provider", String(80), nullable=False, server_default=""), Column("description", String(160), nullable=False, server_default=""),
    Column("inbound_extension", String(3), nullable=False, server_default=""), Column("default_outbound", Integer, nullable=False, server_default="0"),
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
    Column("next_attempt_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")), Column("delivered_at", String(40)), *_timestamps())
api_keys = Table("api_keys", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("name", String(80), unique=True, nullable=False),
    Column("prefix", String(20), nullable=False), Column("key_hash", String(64), unique=True, nullable=False),
    Column("scopes", String(500), nullable=False, server_default="*"), Column("active", Integer, nullable=False, server_default="1"),
    Column("owner_user_id", BigInteger), Column("last_used_at", String(40)), Column("created_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
api_idempotency = Table("api_idempotency", metadata,
    Column("client", String(100), primary_key=True), Column("request_key", String(128), primary_key=True),
    Column("call_id", String(128)), Column("created_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
email_config = Table("email_config", metadata, Column("id", Integer, primary_key=True), Column("sendgrid_api_key_enc", Text, nullable=False),
    Column("from_email", String(254), nullable=False, server_default=""), Column("from_name", String(120), nullable=False, server_default="EIP Telephony Voicemail"),
    Column("enabled", Integer, nullable=False, server_default="0"), Column("updated_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
voicemail_deliveries = Table("voicemail_deliveries", metadata,
    Column("fingerprint", String(128), primary_key=True), Column("mailbox", String(3), nullable=False), Column("recipient", String(254), nullable=False),
    Column("status", String(20), nullable=False, server_default="pending"), Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text), Column("delivered_at", String(40)), Column("updated_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
billing_invoices = Table("billing_invoices", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("user_id", BigInteger, nullable=False), Column("number", String(16), nullable=False),
    Column("period_start", String(10), nullable=False), Column("period_end", String(10), nullable=False), Column("amount_cents", Integer, nullable=False),
    Column("status", String(20), nullable=False, server_default="open"), Column("due_at", String(10), nullable=False), Column("paid_at", String(40)),
    Column("created_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
customer_requests = Table("customer_requests", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("user_id", BigInteger, nullable=False),
    Column("request_type", String(40), nullable=False, server_default="number"), Column("details", Text, nullable=False),
    Column("status", String(20), nullable=False, server_default="pending"), Column("admin_note", Text),
    Column("resolved_at", String(40)), *_timestamps())
customer_sip_accounts = Table("customer_sip_accounts", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("owner_user_id", BigInteger, nullable=False),
    Column("label", String(120), nullable=False), Column("sip_username", String(100), unique=True, nullable=False),
    Column("sip_password_enc", Text, nullable=False), Column("server", String(255), nullable=False),
    Column("port", Integer, nullable=False, server_default="5060"), Column("transport", String(10), nullable=False, server_default="udp"),
    Column("phone_number", String(16), nullable=False, server_default=""), Column("extension", String(3), nullable=False, server_default=""),
    Column("registration_status", String(20), nullable=False, server_default="offline"), Column("last_registered_at", String(40)),
    Column("active", Integer, nullable=False, server_default="1"), *_timestamps())
call_routes = Table("call_routes", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("owner_user_id", BigInteger, nullable=False),
    Column("phone_number", String(16), unique=True, nullable=False), Column("name", String(120), nullable=False, server_default="Main call flow"),
    Column("route_json", Text, nullable=False), Column("active", Integer, nullable=False, server_default="1"), *_timestamps())
activity_history = Table("activity_history", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("owner_user_id", BigInteger),
    Column("actor_user_id", BigInteger), Column("action", String(80), nullable=False), Column("resource_type", String(40), nullable=False),
    Column("resource_id", String(128)), Column("description", String(500), nullable=False),
    Column("created_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
notifications = Table("notifications", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True), Column("user_id", BigInteger, nullable=False),
    Column("kind", String(40), nullable=False), Column("title", String(160), nullable=False), Column("message", String(1000), nullable=False),
    Column("read_at", String(40)), Column("created_at", String(40), nullable=False, server_default=text("CURRENT_TIMESTAMP")))
calls = Table("calls", metadata,
    Column("call_id", String(128), primary_key=True), Column("contact_id", String(255)), Column("member_id", String(255)),
    Column("extension", String(3), nullable=False), Column("phone", String(16), nullable=False), Column("caller_id_number", String(16)),
    Column("provider", String(80)), Column("direction", String(20), nullable=False), Column("status", String(40), nullable=False),
    Column("answered", Integer, nullable=False, server_default="0"), Column("started_at", String(40), nullable=False),
    Column("answered_at", String(40)), Column("ended_at", String(40)), Column("duration_seconds", Integer, nullable=False, server_default="0"),
    Column("employee_channel_id", String(255)), Column("customer_channel_id", String(255)), Column("bridge_id", String(255)),
    Column("recording_name", String(255)), Column("recording_format", String(20)), Column("recording_status", String(40)),
    Column("recording_path", Text), Column("disposition", String(80)), Column("notes", Text))
Index("idx_calls_employee_channel", calls.c.employee_channel_id)
Index("idx_calls_customer_channel", calls.c.customer_channel_id)
Index("idx_calls_recording_name", calls.c.recording_name)
Index("idx_calls_started_at", calls.c.started_at)


class DBRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


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
            return DBRow(row)
        names = [item[0] for item in self.cursor.description or ()]
        return DBRow(zip(names, row))

    def fetchall(self):
        rows = self.cursor.fetchall()
        names = [item[0] for item in self.cursor.description or ()]
        return [DBRow(row) if isinstance(row, dict) else DBRow(zip(names, row)) for row in rows]


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

    def create_all(self):
        if self.is_mysql:
            metadata.create_all(self.engine)

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
