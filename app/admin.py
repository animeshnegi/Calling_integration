from __future__ import annotations

import base64
import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import string
import time
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from flask import Response, current_app, jsonify, redirect, request, session, send_file, send_from_directory, stream_with_context
from werkzeug.security import check_password_hash, generate_password_hash
from pymysql.err import IntegrityError as MySQLIntegrityError

from .database import Database

DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError, MySQLIntegrityError)
INPUT_DB_ERRORS = (ValueError, TypeError, sqlite3.IntegrityError, MySQLIntegrityError)


_LOGIN_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_WINDOW = 15 * 60
_LOGIN_LIMIT = 8


class SettingsStore:
    def __init__(self, path: str, secret_key: str):
        uri = path if "://" in path else f"sqlite:///{path}"
        self.database = Database(uri)
        self.path = Path(path) if not self.database.is_mysql else None
        self.secret_key = secret_key.encode("utf-8")
        self._init_db()

    def _connect(self):
        return self.database.connect()

    def _init_db(self):
        if self.database.is_mysql:
            self.database.create_all()
            existing_user_columns = self.database.columns("admin_users")
            with self._connect() as db:
                self.normalise_sip_usernames(db)
                try:
                    db.execute("CREATE UNIQUE INDEX idx_extensions_sip_username ON extensions(sip_username)")
                except Exception:
                    pass
                for column, definition in (
                    ("full_name", "VARCHAR(120) NOT NULL DEFAULT ''"),
                    ("company_name", "VARCHAR(160) NOT NULL DEFAULT ''"),
                    ("job_role", "VARCHAR(120) NOT NULL DEFAULT ''"),
                    ("phone", "VARCHAR(30) NOT NULL DEFAULT ''"),
                ):
                    if column not in existing_user_columns:
                        db.execute(f"ALTER TABLE admin_users ADD COLUMN {column} {definition}")
                # One-time migration. The platform switch is a veto an
                # administrator holds, and a fresh install allows recording:
                # nothing records until a device's own switch is on, so the
                # permissive default costs no privacy and keeps each customer's
                # control meaningful. An explicit choice is never rewritten.
                if not db.execute("SELECT 1 FROM settings WHERE `key`='recording_policy_v2_initialized'").fetchone():
                    db.execute("INSERT INTO settings(`key`,value) VALUES('recording_enabled','true') ON DUPLICATE KEY UPDATE value='true',updated_at=CURRENT_TIMESTAMP")
                    db.execute("INSERT IGNORE INTO settings(`key`,value) VALUES('recording_policy_v2_initialized','true')")
            return
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, email TEXT NOT NULL DEFAULT '',
                    extension TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user', active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS extensions (
                    extension TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', sip_username TEXT NOT NULL,
                    sip_password_enc TEXT NOT NULL, webrtc_enabled INTEGER NOT NULL DEFAULT 0,
                    recording_enabled INTEGER NOT NULL DEFAULT 1, voicemail_enabled INTEGER NOT NULL DEFAULT 0,
                    voicemail_pin_enc TEXT NOT NULL DEFAULT '', voicemail_email TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS phone_numbers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT NOT NULL UNIQUE, provider TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '', inbound_extension TEXT NOT NULL DEFAULT '',
                    default_outbound INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sip_providers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, server TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 5060, username TEXT NOT NULL DEFAULT '', password_enc TEXT NOT NULL DEFAULT '',
                    transport TEXT NOT NULL DEFAULT 'udp', codecs TEXT NOT NULL DEFAULT 'ulaw,alaw',
                    allowed_ips TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS webhook_endpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
                    token_enc TEXT NOT NULL DEFAULT '', events TEXT NOT NULL DEFAULT '*', active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    id TEXT PRIMARY KEY, endpoint_id INTEGER NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, next_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    delivered_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL DEFAULT '*', active INTEGER NOT NULL DEFAULT 1,
                    last_used_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS api_idempotency (
                    client TEXT NOT NULL, request_key TEXT NOT NULL, call_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(client,request_key)
                );
                CREATE TABLE IF NOT EXISTS email_config (
                    id INTEGER PRIMARY KEY CHECK(id=1), sendgrid_api_key_enc TEXT NOT NULL DEFAULT '',
                    from_email TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL DEFAULT 'EngineerIP Voicemail',
                    enabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS voicemail_deliveries (
                    fingerprint TEXT PRIMARY KEY, mailbox TEXT NOT NULL, recipient TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, delivered_at TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(sip_providers)").fetchall()}
            if "allowed_ips" not in columns:
                db.execute("ALTER TABLE sip_providers ADD COLUMN allowed_ips TEXT NOT NULL DEFAULT ''")
            number_columns = {row["name"] for row in db.execute("PRAGMA table_info(phone_numbers)").fetchall()}
            if "default_outbound" not in number_columns:
                db.execute("ALTER TABLE phone_numbers ADD COLUMN default_outbound INTEGER NOT NULL DEFAULT 0")
            user_columns = {row["name"] for row in db.execute("PRAGMA table_info(admin_users)").fetchall()}
            if "email" not in user_columns:
                db.execute("ALTER TABLE admin_users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            if "extension" not in user_columns:
                db.execute("ALTER TABLE admin_users ADD COLUMN extension TEXT NOT NULL DEFAULT ''")
            for definition in (
                "full_name TEXT NOT NULL DEFAULT ''", "company_name TEXT NOT NULL DEFAULT ''",
                "job_role TEXT NOT NULL DEFAULT ''", "phone TEXT NOT NULL DEFAULT ''",
            ):
                if definition.split()[0] not in user_columns:
                    db.execute(f"ALTER TABLE admin_users ADD COLUMN {definition}")
            extension_columns = {row["name"] for row in db.execute("PRAGMA table_info(extensions)").fetchall()}
            if "voicemail_enabled" not in extension_columns:
                db.execute("ALTER TABLE extensions ADD COLUMN voicemail_enabled INTEGER NOT NULL DEFAULT 0")
            if "voicemail_pin_enc" not in extension_columns:
                db.execute("ALTER TABLE extensions ADD COLUMN voicemail_pin_enc TEXT NOT NULL DEFAULT ''")
            if "voicemail_email" not in extension_columns:
                db.execute("ALTER TABLE extensions ADD COLUMN voicemail_email TEXT NOT NULL DEFAULT ''")
            # Multi-tenant ownership. NULL means a platform-owned legacy resource.
            for table, definition in (
                ("extensions", "owner_user_id INTEGER"),
                ("phone_numbers", "owner_user_id INTEGER"),
                ("webhook_endpoints", "owner_user_id INTEGER"),
                ("api_keys", "owner_user_id INTEGER"),
            ):
                column = definition.split()[0]
                columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
            number_columns = {row["name"] for row in db.execute("PRAGMA table_info(phone_numbers)").fetchall()}
            for definition in (
                "monthly_price_cents INTEGER NOT NULL DEFAULT 500",
                "billing_start TEXT NOT NULL DEFAULT ''",
                "billing_cycle_day INTEGER NOT NULL DEFAULT 1",
                "discontinue_at TEXT NOT NULL DEFAULT ''",
            ):
                if definition.split()[0] not in number_columns:
                    db.execute(f"ALTER TABLE phone_numbers ADD COLUMN {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS billing_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, number TEXT NOT NULL,
                period_start TEXT NOT NULL, period_end TEXT NOT NULL, amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', due_at TEXT NOT NULL, paid_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS customer_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, request_type TEXT NOT NULL DEFAULT 'number',
                    details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', admin_note TEXT, resolved_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS customer_sip_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER NOT NULL, label TEXT NOT NULL,
                    sip_username TEXT NOT NULL UNIQUE, sip_password_enc TEXT NOT NULL, server TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 5060, transport TEXT NOT NULL DEFAULT 'udp', phone_number TEXT NOT NULL DEFAULT '',
                    extension TEXT NOT NULL DEFAULT '', registration_status TEXT NOT NULL DEFAULT 'offline', last_registered_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS call_routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER NOT NULL, phone_number TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL DEFAULT 'Main call flow', route_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS extension_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER NOT NULL,
                    name TEXT NOT NULL, members TEXT NOT NULL DEFAULT '', timeout INTEGER NOT NULL DEFAULT 25,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(owner_user_id, name)
                );
                CREATE TABLE IF NOT EXISTS routing_flows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER NOT NULL,
                    target_type TEXT NOT NULL DEFAULT 'extension', target TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT 'Call flow', route_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(owner_user_id, target_type, target)
                );
                CREATE TABLE IF NOT EXISTS activity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER, actor_user_id INTEGER, action TEXT NOT NULL,
                    resource_type TEXT NOT NULL, resource_id TEXT, description TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
                    message TEXT NOT NULL, read_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Extensions authenticate with a generated identity, so the column is
            # unique platform-wide. Rows written before that rule - including the
            # older "username is the extension number" shape - are normalised here;
            # if duplicates still exist the index is skipped rather than breaking
            # startup, and save_extension keeps writing canonical values.
            self.normalise_sip_usernames(db)
            try:
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_extensions_sip_username ON extensions(sip_username)")
            except Exception:
                pass
            # One-time migration. The platform switch is a veto an administrator
            # holds, and a fresh install allows recording: nothing records until a
            # device's own switch is on, so the permissive default costs no privacy
            # and keeps each customer's control meaningful. An explicit choice is
            # never rewritten.
            if not db.execute("SELECT 1 FROM settings WHERE `key`='recording_policy_v2_initialized'").fetchone():
                db.execute("INSERT INTO settings(`key`,value) VALUES('recording_enabled','true') ON CONFLICT(key) DO UPDATE SET value='true',updated_at=CURRENT_TIMESTAMP")
                db.execute("INSERT INTO settings(`key`,value) VALUES('recording_policy_v2_initialized','true')")

    def ensure_bootstrap_admin(self, username, password):
        if not username or not password:
            return
        with self._connect() as db:
            if db.execute("SELECT id FROM admin_users LIMIT 1").fetchone() is None:
                db.execute(
                    "INSERT INTO admin_users(username,password_hash,role) VALUES(?,?,'admin')",
                    (username, generate_password_hash(password, method="scrypt")),
                )

    def bootstrap_telephony(self, config):
        with self._connect() as db:
            if db.execute("SELECT 1 FROM settings WHERE `key`='bootstrap_complete'").fetchone():
                # Complete an older bootstrap without overwriting administrator changes.
                allowed_ips = os.getenv("IPCOMMS_ALLOWED_IPS", "").strip()
                if allowed_ips:
                    db.execute(
                        "UPDATE sip_providers SET allowed_ips=? WHERE name='IPComms' AND (allowed_ips='' OR allowed_ips IS NULL)",
                        (self._validate_allowed_ips(allowed_ips),),
                    )
                return
            for ext in config.ASTERISK_EXTENSIONS:
                password = os.getenv(f"EXTENSION_{ext}_PASSWORD", "")
                if password:
                    db.execute(
                        """INSERT OR IGNORE INTO extensions
                        (extension,display_name,sip_username,sip_password_enc,webrtc_enabled,recording_enabled,active)
                        VALUES(?,?,?,?,?,?,1)""",
                        (ext, "", ext, self.encrypt(password), 0, 0),
                    )
            did = os.getenv("IPCOMMS_DID", "").strip()
            if did:
                db.execute(
                    """INSERT OR IGNORE INTO phone_numbers
                    (number,provider,description,inbound_extension,active) VALUES(?,?,?,?,1)""",
                    (did, "IPComms", "Primary DID", config.DEFAULT_EXTENSION),
                )
            if os.getenv("IPCOMMS_SIP_SERVER") and os.getenv("IPCOMMS_SIP_USERNAME"):
                existing = db.execute("SELECT id FROM sip_providers WHERE name='IPComms'").fetchone()
                if existing is None:
                    db.execute(
                        """INSERT INTO sip_providers(name,server,port,username,password_enc,transport,codecs,allowed_ips,active)
                        VALUES(?,?,?,?,?,?,?,?,1)""",
                        (
                            "IPComms",
                            os.getenv("IPCOMMS_SIP_SERVER", ""),
                            int(os.getenv("IPCOMMS_SIP_PORT", "5060")),
                            os.getenv("IPCOMMS_SIP_USERNAME", ""),
                            self.encrypt(os.getenv("IPCOMMS_SIP_PASSWORD", "")),
                            "udp",
                            "ulaw,alaw",
                            self._validate_allowed_ips(os.getenv("IPCOMMS_ALLOWED_IPS", "")),
                        ),
                    )
            db.execute("INSERT INTO settings(`key`,value) VALUES('bootstrap_complete','true')")

    def authenticate(self, username, password):
        with self._connect() as db:
            row = db.execute(
                "SELECT id,username,email,extension,role,password_hash FROM admin_users WHERE username=? AND active=1",
                (username,),
            ).fetchone()
        if not row or not check_password_hash(row["password_hash"], password):
            return None
        return {"id": row["id"], "username": row["username"], "email": row["email"], "extension": row["extension"], "role": row["role"]}

    def get_user(self, user_id):
        with self._connect() as db:
            row = db.execute("SELECT id,username,email,extension,role,active FROM admin_users WHERE id=?", (int(user_id),)).fetchone()
        return dict(row) if row else None

    def set_admin_password(self, user_id, password):
        with self._connect() as db:
            db.execute(
                "UPDATE admin_users SET password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (generate_password_hash(password, method="scrypt"), user_id),
            )

    def list_extensions(self, owner_user_id: int | None = None):
        with self._connect() as db:
            where = " WHERE owner_user_id=?" if owner_user_id is not None else ""
            rows = db.execute(
                f"SELECT extension,display_name,sip_username,webrtc_enabled,recording_enabled,voicemail_enabled,voicemail_email,active,owner_user_id FROM extensions{where} ORDER BY extension",
                (int(owner_user_id),) if owner_user_id is not None else (),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_extension_owner(self, extension: str) -> int | None:
        with self._connect() as db:
            row = db.execute("SELECT owner_user_id FROM extensions WHERE extension=?", (str(extension),)).fetchone()
        return row["owner_user_id"] if row else None

    def get_extension_password(self, extension):
        with self._connect() as db:
            row = db.execute("SELECT sip_password_enc FROM extensions WHERE extension=?", (str(extension),)).fetchone()
        return self.decrypt(row["sip_password_enc"]) if row else ""

    def get_voicemail_pin(self, extension):
        with self._connect() as db:
            row = db.execute("SELECT voicemail_pin_enc FROM extensions WHERE extension=?", (str(extension),)).fetchone()
        return self.decrypt(row["voicemail_pin_enc"]) if row and row["voicemail_pin_enc"] else ""

    @staticmethod
    def _validate_config_value(value: str, field: str, max_length: int = 255) -> str:
        value = str(value or "").strip()
        if not value or len(value) > max_length or any(char in value for char in "\r\n;#"):
            raise ValueError(f"Invalid {field}")
        return value

    @staticmethod
    def _validate_provider_server(value: str) -> str:
        value = str(value or "").strip()
        if not value or len(value) > 255 or any(char in value for char in "\r\n;/\\@ "):
            raise ValueError("Invalid provider server")
        return value

    def save_extension(self, data, owner_user_id: int | None = None, enforce_owner: bool = False):
        extension = str(data.get("extension", "")).strip()
        if not extension.isdigit() or not 100 <= int(extension) <= 999:
            raise ValueError("Extension must be a 3-digit number from 100 to 999")
        password = str(data.get("sip_password") or "")
        if any(char in password for char in "\r\n;#"):
            raise ValueError("Invalid SIP password")
        voicemail_enabled_value = data.get("voicemail_enabled")
        voicemail_pin = str(data.get("voicemail_pin") or "")
        voicemail_email = str(data.get("voicemail_email") or "").strip().lower()
        if voicemail_pin and not re.fullmatch(r"\d{4,10}", voicemail_pin):
            raise ValueError("Voicemail PIN must contain 4 to 10 digits")
        if voicemail_email and (len(voicemail_email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", voicemail_email)):
            raise ValueError("Voicemail notification email is invalid")
        with self._connect() as db:
            existing = db.execute(
                "SELECT sip_username,sip_password_enc,voicemail_enabled,voicemail_pin_enc,owner_user_id FROM extensions WHERE extension=?", (extension,)
            ).fetchone()
            created = existing is None
            if existing and enforce_owner and owner_user_id is not None and existing["owner_user_id"] != int(owner_user_id):
                raise ValueError("Extension belongs to another customer")
            # Devices authenticate with this name, so it is derived here and never
            # accepted from a caller: six random letters, an underscore, the
            # extension. Editing an extension keeps the identity a registered
            # phone already uses; a row in any other shape (including the older
            # "username is the number" form) is replaced.
            username = self.canonical_sip_username(extension, existing["sip_username"] if existing else "")
            voicemail_enabled = bool(voicemail_enabled_value) if voicemail_enabled_value is not None else bool(existing and existing["voicemail_enabled"])
            # A new extension provisions its own SIP credentials, so a customer
            # can create an extension and register a device without inventing a
            # password first. An explicit password always wins, and editing
            # without one keeps the existing secret.
            generated = False
            if password:
                encrypted = self.encrypt(password)
            elif existing:
                encrypted = existing["sip_password_enc"]
            else:
                encrypted = self.encrypt(self.generate_sip_password())
                generated = True
            voicemail_pin_enc = self.encrypt(voicemail_pin) if voicemail_pin else (existing["voicemail_pin_enc"] if existing else "")
            if voicemail_enabled and not voicemail_pin_enc:
                raise ValueError("Voicemail PIN is required when voicemail is enabled")
            db.execute(
                """INSERT INTO extensions(extension,display_name,sip_username,sip_password_enc,webrtc_enabled,recording_enabled,voicemail_enabled,voicemail_pin_enc,voicemail_email,active,owner_user_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(extension) DO UPDATE SET display_name=excluded.display_name,sip_username=excluded.sip_username,
                sip_password_enc=excluded.sip_password_enc,webrtc_enabled=excluded.webrtc_enabled,recording_enabled=excluded.recording_enabled,
                voicemail_enabled=excluded.voicemail_enabled,voicemail_pin_enc=excluded.voicemail_pin_enc,voicemail_email=excluded.voicemail_email,
                active=excluded.active,owner_user_id=excluded.owner_user_id,updated_at=CURRENT_TIMESTAMP""",
                (
                    extension,
                    str(data.get("display_name", "")).strip()[:120],
                    username,
                    encrypted,
                    int(bool(data.get("webrtc_enabled"))),
                    int(bool(data.get("recording_enabled", False))),
                    int(voicemail_enabled),
                    voicemail_pin_enc,
                    voicemail_email,
                    int(bool(data.get("active", True))),
                    int(owner_user_id) if owner_user_id is not None else (int(data["owner_user_id"]) if str(data.get("owner_user_id", "")).isdigit() else None),
                ),
            )
        owner = owner_user_id if owner_user_id is not None else (existing["owner_user_id"] if existing else None)
        # Every extension gets a working call flow of its own. It is only written
        # when the extension is created, so a flow the customer later edits is
        # never overwritten.
        if created and owner is not None and int(bool(data.get("active", True))):
            self.ensure_extension_flow(int(owner), extension, voicemail=bool(voicemail_enabled))
            self.sync_primary_flows(int(owner), extension)
        return extension

    def primary_extension(self, owner_user_id: int) -> str:
        """The device a customer's main line belongs to: their lowest extension,
        which is the one auto-provisioning created first."""
        extensions = sorted(
            (row["extension"] for row in self.list_extensions(owner_user_id) if row["active"]),
            key=lambda value: int(value),
        )
        return extensions[0] if extensions else ""

    def sync_primary_flows(self, owner_user_id: int, extension: str) -> None:
        """A main line rings every device, so adding one extends it.

        Only a flow that is still the generated one is rewritten: a single ring
        step, the default timeout, no group, and members that are a subset of the
        customer's own devices. Anything the customer has designed - extra steps,
        another timeout, a group - is left exactly as it is.
        """
        owner_user_id, extension = int(owner_user_id), str(extension)
        primary = self.primary_extension(owner_user_id)
        devices = sorted(
            (row["extension"] for row in self.list_extensions(owner_user_id) if row["active"]),
            key=lambda value: int(value),
        )
        if not primary or len(devices) < 2 or extension not in devices:
            return
        primary_number = self.primary_number(owner_user_id)
        if not primary_number:
            return
        flow = next((row for row in self.list_call_routes(owner_user_id) if row["phone_number"] == primary_number), None)
        if not flow:
            return
        nodes = (flow.get("route") or {}).get("nodes") or []
        if not self._is_generated_ring(nodes, devices):
            return
        mailbox = ""
        if len(nodes) == 2 and str(nodes[1].get("type")) == "voicemail":
            mailbox = str(nodes[1].get("mailbox") or "")
        self.save_call_route(owner_user_id, {
            "phone_number": primary_number, "name": flow.get("name") or "Main call flow",
            "route": self.default_number_route(devices, voicemail=mailbox), "active": bool(flow.get("active", True)),
        })

    @staticmethod
    def _is_generated_ring(nodes, devices: list[str]) -> bool:
        """Is this flow still the ring-the-devices default the platform wrote?"""
        if not nodes or not isinstance(nodes[0], dict):
            return False
        head = nodes[0]
        if str(head.get("type")) != "ring_group" or head.get("group_id") or not head.get("configured"):
            return False
        if int(head.get("timeout") or 0) != 25:
            return False
        members = {str(value) for value in (head.get("extensions") or [])}
        if not members or not members <= set(devices):
            return False
        if len(nodes) == 1:
            return True
        return len(nodes) == 2 and str(nodes[1].get("type")) == "voicemail"

    def primary_number(self, owner_user_id: int) -> str:
        """The customer's main line: the number the auto-provisioned device owns."""
        numbers = [row for row in self.list_numbers(int(owner_user_id)) if row["active"]]
        primary = self.primary_extension(owner_user_id)
        match = next((row for row in numbers if primary and row["inbound_extension"] == primary), None)
        return (match or (numbers[0] if numbers else None) or {}).get("number", "")


    # Six letters, an underscore, the extension (`KUDGTE_101`): the identity a
    # device authenticates with. The letters are random so a username cannot be
    # guessed from an extension, and the suffix keeps it recognisable in Asterisk
    # and in the log. A row minted before the letters became upper case is still
    # accepted as canonical: renaming an identity would stop the phone that has
    # it from registering until somebody reconfigured it.
    SIP_USERNAME_RE = re.compile(r"^[A-Za-z]{6}_\d{3}$")

    @classmethod
    def generate_sip_username(cls, extension: str) -> str:
        """`KUDGTE_101` - six random upper-case letters and the extension."""
        stem = "".join(secrets.choice(string.ascii_uppercase) for _ in range(6))
        return f"{stem}_{str(extension).strip()}"

    @classmethod
    def canonical_sip_username(cls, extension: str, current: str = "") -> str:
        """Keep an identity that already has the right shape, mint one otherwise.

        Editing an extension must not silently change the password a phone logs in
        with, so a canonical value is reused; anything else (a row from before this
        rule, a caller-supplied name) is replaced.
        """
        current = str(current or "")
        if cls.SIP_USERNAME_RE.match(current) and current.endswith(f"_{str(extension).strip()}"):
            return current
        return cls.generate_sip_username(extension)

    def normalise_sip_usernames(self, db=None) -> int:
        """Give every extension the canonical SIP identity, once, at startup.

        Linked device accounts inherit the identity of the extension they register
        for, so the two never disagree in the generated Asterisk configuration.
        """
        owned = db is None
        if owned:
            db = self._connect()
        try:
            rows = db.execute("SELECT extension,sip_username FROM extensions").fetchall()
            changed = 0
            for row in rows:
                username = self.canonical_sip_username(row["extension"], row["sip_username"])
                if username == str(row["sip_username"] or ""):
                    continue
                db.execute("UPDATE extensions SET sip_username=? WHERE extension=?", (username, row["extension"]))
                db.execute("UPDATE customer_sip_accounts SET sip_username=? WHERE extension=?", (username, str(row["extension"])))
                changed += 1
            return changed
        finally:
            if owned:
                db.close()

    # A person reads this off the credential sheet and types it into a phone, so
    # every class is present and the characters that get misread (`0/O`, `1/l/I`)
    # are left out. `;`, `#` and whitespace are never generated either: the
    # generated Asterisk configuration rejects a value containing them.
    SIP_PASSWORD_CLASSES = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ",
        "abcdefghijkmnopqrstuvwxyz",
        "23456789",
        "!@$%^&*()-_=+?",
    )

    @classmethod
    def generate_sip_password(cls, length: int = 16) -> str:
        """A strong secret: upper case, lower case, digits and symbols."""
        length = max(12, int(length))
        secret = [secrets.choice(chars) for chars in cls.SIP_PASSWORD_CLASSES]
        pool = "".join(cls.SIP_PASSWORD_CLASSES)
        secret += [secrets.choice(pool) for _ in range(length - len(secret))]
        secrets.SystemRandom().shuffle(secret)
        return "".join(secret)

    def next_extension_number(self, owner_user_id: int | None = None) -> str:
        """Lowest free three-digit extension.

        Extension numbers are the primary key, so they are unique platform-wide:
        one customer cannot be handed a number another customer already owns.
        Passing an owner treats that customer's own extensions as reusable.
        """
        with self._connect() as db:
            rows = db.execute("SELECT extension,owner_user_id FROM extensions").fetchall()
        taken = {
            str(row["extension"]) for row in rows
            if owner_user_id is None or row["owner_user_id"] != int(owner_user_id)
        }
        for candidate in range(101, 1000):
            if str(candidate) not in taken:
                return str(candidate)
        raise ValueError("No free extension numbers remain")

    # The address customers point their devices at, and the base their API and
    # webhook examples are built from: this platform's own server, never the
    # carrier trunk it dials out through. An administrator sets it once, as an IP
    # address or a subdomain.
    SERVICE_HOST_RE = re.compile(
        r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
    )

    def service_address(self, fallback_host: str = "") -> dict:
        """Where this deployment answers, and the links built from it.

        An administrator's `service_host` setting wins. Without one the host the
        console is being read from is used, so even an unconfigured deployment
        shows an address a customer can type into a phone instead of the
        carrier's trunk address.
        """
        settings = self.get_settings()
        host = str(settings.get("service_host") or "").strip()
        configured = bool(host)
        if not host:
            host = str(fallback_host or "").strip()
        try:
            port = int(settings.get("service_sip_port") or 5060)
        except (TypeError, ValueError):
            port = 5060
        if not 1 <= port <= 65535:
            port = 5060
        return {
            "host": host,
            "port": port,
            "configured": configured,
            "sip": f"{host}:{port}" if host else "",
            "api_base": f"https://{host}" if host else "",
        }

    def reveal_extension_credentials(self, extension, owner_user_id: int | None = None, fallback_host: str = ""):
        """The SIP credentials a device registers with, plus where to register.

        A device account linked to the extension overrides the extension's own
        secret in the generated Asterisk config, so the effective credential is
        what gets returned.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT extension,display_name,sip_username,sip_password_enc,owner_user_id,voicemail_enabled,active FROM extensions WHERE extension=?",
                (str(extension),),
            ).fetchone()
        if not row or (owner_user_id is not None and row["owner_user_id"] != int(owner_user_id)):
            raise ValueError("Extension not found")
        owner = row["owner_user_id"]
        numbers = [item for item in self.list_numbers(owner) if item["inbound_extension"] == row["extension"] and item["active"]] if owner is not None else []
        device = next(
            (item for item in self.list_sip_accounts(owner, include_password=True)
             if str(item.get("extension") or "") == row["extension"] and item["active"]),
            None,
        )
        # A phone registers with this platform, so the platform's own address is
        # the server that belongs in the sheet. A device account's server and then
        # the carrier's are only used while no address has been set, so an
        # existing deployment keeps answering with what it answered before.
        service = self.service_address(fallback_host)
        provider = None
        if service["host"]:
            server = service["host"]
        elif device:
            server = device["server"]
        else:
            provider = self.get_provider(next((item["provider"] for item in numbers if item["provider"]), None)) or self.get_provider()
            server = (provider or {}).get("server", "")
        return {
            "extension": row["extension"],
            "display_name": row["display_name"],
            "active": bool(row["active"]),
            "sip_username": device["sip_username"] if device else (row["sip_username"] or row["extension"]),
            "sip_password": (device or {}).get("sip_password") or (self.decrypt(row["sip_password_enc"]) if row["sip_password_enc"] else ""),
            "server": server or "",
            "port": int(service["port"] if service["host"] else ((device or provider or {}).get("port") or 5060)),
            "transport": (device or provider or {}).get("transport") or "udp",
            "registration": "device" if device else "extension",
            "device_label": (device or {}).get("label", ""),
            "numbers": [item["number"] for item in numbers],
            "voicemail_enabled": bool(row["voicemail_enabled"]),
            # Carried along so the sheet can offer the whole "register here" value
            # in one piece, and point at the API base, without a second request.
            "registration_address": f"{server}:{int(service['port'] if service['host'] else ((device or provider or {}).get('port') or 5060))}" if server else "",
            "api_base": service["api_base"],
            "managed_address": service["configured"],
        }

    def set_extension_recording(self, extension: str, enabled: bool):
        extension = str(extension).strip()
        with self._connect() as db:
            result = db.execute(
                "UPDATE extensions SET recording_enabled=?,updated_at=CURRENT_TIMESTAMP WHERE extension=? AND active=1",
                (int(bool(enabled)), extension),
            )
            if result.rowcount == 0:
                raise ValueError("Active extension not found")

    def delete_extension(self, extension, owner_user_id: int | None = None):
        extension = str(extension).strip()
        with self._connect() as db:
            if owner_user_id is not None and not db.execute(
                "SELECT 1 FROM extensions WHERE extension=? AND owner_user_id=?", (extension, int(owner_user_id))
            ).fetchone():
                raise ValueError("Extension not found")
            if db.execute("SELECT 1 FROM phone_numbers WHERE inbound_extension=?", (extension,)).fetchone():
                raise ValueError("Cannot delete an extension used by an inbound DID")
            if self.extension_is_a_call_default(extension):
                raise ValueError("Cannot delete an extension used as a call default; change Call defaults first")
            result = db.execute("DELETE FROM extensions WHERE extension=?", (extension,))
            if result.rowcount == 0:
                raise ValueError("Extension not found")
            # Take the extension out of every group and drop the flow written for
            # it, so no group or call flow is left pointing at a gone extension.
            for group in db.execute("SELECT id,members FROM extension_groups").fetchall():
                members = [ext for ext in str(group["members"] or "").split(",") if ext and ext != extension]
                if len(members) != len(str(group["members"] or "").split(",")):
                    db.execute("UPDATE extension_groups SET members=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (",".join(members), group["id"]))
            db.execute("DELETE FROM routing_flows WHERE target_type='extension' AND target=?", (extension,))

    def list_numbers(self, owner_user_id: int | None = None):
        with self._connect() as db:
            db.execute("UPDATE phone_numbers SET active=0,updated_at=CURRENT_TIMESTAMP WHERE discontinue_at!='' AND date(discontinue_at)<=date('now') AND active=1")
            where = " WHERE owner_user_id=?" if owner_user_id is not None else ""
            rows = db.execute(
                f"SELECT id,number,provider,description,inbound_extension,default_outbound,active,owner_user_id,monthly_price_cents,billing_start,billing_cycle_day,discontinue_at FROM phone_numbers{where} ORDER BY number",
                (int(owner_user_id),) if owner_user_id is not None else (),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_number(self, data):
        number = str(data.get("number", "")).strip()
        if not number.startswith("+") or not number[1:].isdigit() or not 8 <= len(number) <= 16:
            raise ValueError("Phone number must be in E.164 format")
        inbound = str(data.get("inbound_extension", "")).strip()
        owner_user_id = int(data["owner_user_id"]) if str(data.get("owner_user_id", "")).isdigit() else None
        try:
            price_cents = max(0, int(round(float(data.get("monthly_price", 5)) * 100)))
            cycle_day = min(28, max(1, int(data.get("billing_cycle_day", 1))))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid billing price or cycle day") from exc
        with self._connect() as db:
            if owner_user_id and not db.execute("SELECT 1 FROM admin_users WHERE id=? AND role='user' AND active=1", (owner_user_id,)).fetchone():
                raise ValueError("Customer account is not active")
            if inbound and not db.execute(
                "SELECT 1 FROM extensions WHERE extension=? AND active=1 AND (owner_user_id=? OR (owner_user_id IS NULL AND ? IS NULL))", (inbound, owner_user_id, owner_user_id)
            ).fetchone():
                raise ValueError("Inbound extension must belong to the selected customer")
            if data.get("provider") and not db.execute(
                "SELECT 1 FROM sip_providers WHERE name=? AND active=1", (str(data.get("provider")).strip(),)
            ).fetchone():
                raise ValueError("Provider must be an active configured SIP provider")
            default_outbound = bool(data.get("default_outbound"))
            if default_outbound and not inbound:
                raise ValueError("A default outbound number must be assigned to an extension")
            if default_outbound:
                db.execute("UPDATE phone_numbers SET default_outbound=0,updated_at=CURRENT_TIMESTAMP WHERE inbound_extension=?", (inbound,))
            db.execute(
                """INSERT INTO phone_numbers(number,provider,description,inbound_extension,default_outbound,active,owner_user_id,monthly_price_cents,billing_start,billing_cycle_day,discontinue_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(number) DO UPDATE SET provider=excluded.provider,description=excluded.description,
                inbound_extension=excluded.inbound_extension,default_outbound=excluded.default_outbound,
                active=excluded.active,owner_user_id=excluded.owner_user_id,monthly_price_cents=excluded.monthly_price_cents,
                billing_start=excluded.billing_start,billing_cycle_day=excluded.billing_cycle_day,discontinue_at=excluded.discontinue_at,
                updated_at=CURRENT_TIMESTAMP""",
                (
                    number,
                    str(data.get("provider", "")).strip()[:80],
                    str(data.get("description", "")).strip()[:160],
                    inbound[:3],
                    int(default_outbound),
                    int(bool(data.get("active", True))),
                    owner_user_id,
                    price_cents,
                    str(data.get("billing_start", "")).strip()[:10],
                    cycle_day,
                    str(data.get("discontinue_at", "")).strip()[:10],
                ),
            )
        return number

    def get_outbound_number(self, extension: str, requested: str | None = None):
        with self._connect() as db:
            if requested:
                row = db.execute("SELECT number,provider FROM phone_numbers WHERE number=? AND inbound_extension=? AND active=1", (requested, extension)).fetchone()
            else:
                row = db.execute("SELECT number,provider FROM phone_numbers WHERE inbound_extension=? AND active=1 ORDER BY default_outbound DESC,id LIMIT 1", (extension,)).fetchone()
        return dict(row) if row else None

    def set_default_outbound_number(self, extension: str, number: str):
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM phone_numbers WHERE number=? AND inbound_extension=? AND active=1", (number, extension)).fetchone():
                raise ValueError("Number is not assigned to this extension")
            db.execute("UPDATE phone_numbers SET default_outbound=CASE WHEN number=? THEN 1 ELSE 0 END,updated_at=CURRENT_TIMESTAMP WHERE inbound_extension=?", (number, extension))

    def delete_number(self, number):
        number = str(number).strip()
        with self._connect() as db:
            result = db.execute("DELETE FROM phone_numbers WHERE number=?", (number,))
            if result.rowcount == 0:
                raise ValueError("Phone number not found")
            db.execute("DELETE FROM call_routes WHERE phone_number=?", (number,))

    def ensure_monthly_invoices(self):
        today = date.today()
        created = []
        with self._connect() as db:
            numbers = db.execute("SELECT number,owner_user_id,monthly_price_cents,billing_start,billing_cycle_day FROM phone_numbers WHERE owner_user_id IS NOT NULL AND active=1 AND (discontinue_at='' OR date(discontinue_at)>date('now'))").fetchall()
            for item in numbers:
                cycle_day = min(28, max(1, int(item["billing_cycle_day"] or 1)))
                year, month = today.year, today.month
                if today.day < cycle_day:
                    month -= 1
                    if month == 0:
                        year, month = year - 1, 12
                start = date(year, month, min(cycle_day, monthrange(year, month)[1]))
                next_month = month + 1
                next_year = year
                if next_month == 13:
                    next_year, next_month = year + 1, 1
                end = date(next_year, next_month, min(cycle_day, monthrange(next_year, next_month)[1])) - timedelta(days=1)
                configured_start = item["billing_start"]
                if configured_start and configured_start > today.isoformat():
                    continue
                exists = db.execute("SELECT 1 FROM billing_invoices WHERE number=? AND period_start=?", (item["number"], start.isoformat())).fetchone()
                if not exists:
                    due_at = (start + timedelta(days=7)).isoformat()
                    db.execute("INSERT INTO billing_invoices(user_id,number,period_start,period_end,amount_cents,due_at) VALUES(?,?,?,?,?,?)", (item["owner_user_id"], item["number"], start.isoformat(), end.isoformat(), item["monthly_price_cents"], due_at))
                    created.append((int(item["owner_user_id"]), item["number"], due_at, int(item["monthly_price_cents"])))
        for user_id, number, due_at, amount_cents in created:
            self.add_notification(user_id, "payment", "Payment reminder", f"Your ${amount_cents / 100:.2f} invoice for {number} is due {due_at}. Payment is handled externally.")

    def request_number_discontinuation(self, number: str, user_id: int) -> str:
        today = date.today()
        with self._connect() as db:
            row = db.execute("SELECT billing_cycle_day FROM phone_numbers WHERE number=? AND owner_user_id=? AND active=1", (str(number), int(user_id))).fetchone()
            if not row:
                raise ValueError("Active phone number not found")
            cycle_day = min(28, max(1, int(row["billing_cycle_day"] or 1)))
            year, month = today.year, today.month
            candidate = date(year, month, min(cycle_day, monthrange(year, month)[1]))
            if candidate <= today:
                month += 1
                if month == 13:
                    year, month = year + 1, 1
                candidate = date(year, month, min(cycle_day, monthrange(year, month)[1]))
            db.execute("UPDATE phone_numbers SET discontinue_at=?,updated_at=CURRENT_TIMESTAMP WHERE number=? AND owner_user_id=?", (candidate.isoformat(), str(number), int(user_id)))
            return candidate.isoformat()

    def list_invoices(self, user_id: int | None = None):
        self.ensure_monthly_invoices()
        with self._connect() as db:
            where = " WHERE user_id=?" if user_id is not None else ""
            rows = db.execute(
                f"SELECT id,user_id,number,period_start,period_end,amount_cents,status,due_at,paid_at,created_at FROM billing_invoices{where} ORDER BY created_at DESC",
                (int(user_id),) if user_id is not None else (),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_invoice_status(self, invoice_id: int, status: str):
        if status not in {"open", "paid", "void"}:
            raise ValueError("Invoice status must be open, paid, or void")
        with self._connect() as db:
            result = db.execute("UPDATE billing_invoices SET status=?,paid_at=CASE WHEN ?='paid' THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id=?", (status, status, int(invoice_id)))
            if result.rowcount == 0:
                raise ValueError("Invoice not found")

    def create_invoice(self, user_id: int, number: str, period_start: str, period_end: str, amount_cents: int, due_at: str):
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM phone_numbers WHERE number=? AND owner_user_id=?", (number, int(user_id))).fetchone():
                raise ValueError("Number is not assigned to this customer")
            return db.execute(
                "INSERT INTO billing_invoices(user_id,number,period_start,period_end,amount_cents,due_at) VALUES(?,?,?,?,?,?)",
                (int(user_id), number, period_start, period_end, int(amount_cents), due_at),
            ).lastrowid

    def add_activity(self, owner_user_id, actor_user_id, action, resource_type, resource_id, description):
        with self._connect() as db:
            db.execute("INSERT INTO activity_history(owner_user_id,actor_user_id,action,resource_type,resource_id,description) VALUES(?,?,?,?,?,?)",
                       (owner_user_id, actor_user_id, str(action)[:80], str(resource_type)[:40], str(resource_id or "")[:128], str(description)[:500]))

    def list_activity(self, owner_user_id: int | None = None, limit: int = 100):
        with self._connect() as db:
            where = " WHERE owner_user_id=?" if owner_user_id is not None else ""
            rows = db.execute(f"SELECT id,owner_user_id,actor_user_id,action,resource_type,resource_id,description,created_at FROM activity_history{where} ORDER BY created_at DESC,id DESC LIMIT ?", ((int(owner_user_id), limit) if owner_user_id is not None else (limit,))).fetchall()
        return [dict(row) for row in rows]

    def add_notification(self, user_id: int, kind: str, title: str, message: str):
        with self._connect() as db:
            return db.execute("INSERT INTO notifications(user_id,kind,title,message) VALUES(?,?,?,?)",
                              (int(user_id), str(kind)[:40], str(title)[:160], str(message)[:1000])).lastrowid

    def list_notifications(self, user_id: int):
        with self._connect() as db:
            rows = db.execute("SELECT id,kind,title,message,read_at,created_at FROM notifications WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT 100", (int(user_id),)).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_read(self, notification_id: int, user_id: int):
        with self._connect() as db:
            result = db.execute("UPDATE notifications SET read_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", (int(notification_id), int(user_id)))
            if not result.rowcount:
                raise ValueError("Notification not found")

    def mark_all_notifications_read(self, user_id: int):
        with self._connect() as db:
            db.execute("UPDATE notifications SET read_at=CURRENT_TIMESTAMP WHERE user_id=? AND read_at IS NULL", (int(user_id),))

    def create_request(self, user_id: int, request_type: str, details: str):
        if request_type not in {"number", "access", "routing", "billing"}:
            raise ValueError("Unsupported request type")
        details = str(details).strip()
        if not details or len(details) > 2000:
            raise ValueError("Request details are required and must be under 2000 characters")
        with self._connect() as db:
            pending = db.execute("SELECT id FROM customer_requests WHERE user_id=? AND request_type=? AND status='pending'", (int(user_id), request_type)).fetchone()
            if pending:
                raise ValueError(f"A pending {request_type} request already exists")
            request_id = db.execute("INSERT INTO customer_requests(user_id,request_type,details) VALUES(?,?,?)", (int(user_id), request_type, details)).lastrowid
        self.add_activity(user_id, user_id, "request.created", "request", request_id, f"{request_type.title()} request submitted")
        return request_id

    def list_requests(self, user_id: int | None = None):
        with self._connect() as db:
            where = " WHERE r.user_id=?" if user_id is not None else ""
            rows = db.execute(f"SELECT r.id,r.user_id,r.request_type,r.details,r.status,r.admin_note,r.resolved_at,r.created_at,r.updated_at,u.username,u.company_name FROM customer_requests r JOIN admin_users u ON u.id=r.user_id{where} ORDER BY CASE WHEN r.status='pending' THEN 0 ELSE 1 END,r.created_at DESC", (int(user_id),) if user_id is not None else ()).fetchall()
        return [dict(row) for row in rows]

    def resolve_request(self, request_id: int, status: str, admin_note: str, actor_user_id: int):
        if status not in {"approved", "rejected", "fulfilled"}:
            raise ValueError("Request status must be approved, rejected, or fulfilled")
        with self._connect() as db:
            row = db.execute("SELECT user_id,request_type FROM customer_requests WHERE id=?", (int(request_id),)).fetchone()
            if not row:
                raise ValueError("Request not found")
            db.execute("UPDATE customer_requests SET status=?,admin_note=?,resolved_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, str(admin_note)[:2000], int(request_id)))
        self.add_notification(row["user_id"], "request", f"{row['request_type'].title()} request {status}", admin_note or f"Your request was {status}.")
        self.add_activity(row["user_id"], actor_user_id, f"request.{status}", "request", request_id, f"Request {status}")

    def list_sip_accounts(self, owner_user_id: int | None = None, include_password: bool = False):
        with self._connect() as db:
            where = " WHERE owner_user_id=?" if owner_user_id is not None else ""
            rows = db.execute(f"SELECT id,owner_user_id,label,sip_username,sip_password_enc,server,port,transport,phone_number,extension,registration_status,last_registered_at,active,created_at,updated_at FROM customer_sip_accounts{where} ORDER BY label", (int(owner_user_id),) if owner_user_id is not None else ()).fetchall()
        result = []
        for row in rows:
            item = dict(row); encrypted = item.pop("sip_password_enc")
            item["has_password"] = bool(encrypted)
            if include_password:
                item["sip_password"] = self.decrypt(encrypted)
            result.append(item)
        return result

    def save_sip_account(self, data, owner_user_id: int):
        owner_user_id = int(owner_user_id)
        extension = str(data.get("extension", "")).strip()
        # A device authenticates with this name, so the platform keeps it unique
        # and anchored. Linked to an extension it *is* that extension's generated
        # identity - the customer may not rename the identity their phone logs in
        # with - and no account may take a name an extension already answers to.
        extensions = self.list_extensions()
        if extension:
            username = next(
                (str(row["sip_username"] or "") for row in extensions if str(row["extension"]) == extension),
                "",
            ) or self.generate_sip_username(extension)
        else:
            username = self._validate_config_value(data.get("sip_username"), "SIP username", 100)
            if username in {str(row["extension"]) for row in extensions} or username in {str(row["sip_username"]) for row in extensions}:
                raise ValueError("That SIP username belongs to an extension")
        account_id = int(data["id"]) if str(data.get("id", "")).isdigit() else None
        if any(str(row["sip_username"]) == username and row["id"] != account_id for row in self.list_sip_accounts()):
            raise ValueError("That SIP username is already in use")
        label = self._validate_config_value(data.get("label") or username, "SIP label", 120)
        server = self._validate_provider_server(data.get("server"))
        password = str(data.get("sip_password") or "")
        phone_number = str(data.get("phone_number", "")).strip()
        transport = str(data.get("transport", "udp")).lower()
        port = int(data.get("port", 5060))
        if transport not in {"udp", "tcp", "tls"}:
            raise ValueError("SIP transport must be UDP, TCP, or TLS")
        if not 1 <= port <= 65535:
            raise ValueError("SIP port must be between 1 and 65535")
        if phone_number and not any(row["number"] == phone_number for row in self.list_numbers(owner_user_id)):
            raise ValueError("SIP phone number is not assigned to this customer")
        if extension and not any(row["extension"] == extension for row in self.list_extensions(owner_user_id)):
            raise ValueError("SIP extension is not assigned to this customer")
        with self._connect() as db:
            existing = db.execute("SELECT owner_user_id,sip_password_enc FROM customer_sip_accounts WHERE id=?", (account_id,)).fetchone() if account_id else None
            if existing and existing["owner_user_id"] != owner_user_id:
                raise ValueError("SIP account belongs to another customer")
            encrypted = self.encrypt(password) if password else (existing["sip_password_enc"] if existing else "")
            if not encrypted:
                raise ValueError("SIP password is required")
            values = (label, username, encrypted, server, port, transport, phone_number, extension, int(bool(data.get("active", True))))
            if existing:
                db.execute("UPDATE customer_sip_accounts SET label=?,sip_username=?,sip_password_enc=?,server=?,port=?,transport=?,phone_number=?,extension=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (*values, account_id))
                return account_id
            return db.execute("INSERT INTO customer_sip_accounts(label,sip_username,sip_password_enc,server,port,transport,phone_number,extension,active,owner_user_id) VALUES(?,?,?,?,?,?,?,?,?,?)", (*values, owner_user_id)).lastrowid

    def set_extension_password(self, extension, password: str = "", owner_user_id: int | None = None) -> dict:
        """Rotate the secret a device registers with.

        A linked device account overrides the extension's own password in the
        generated Asterisk configuration, so the effective credential is what
        changes - otherwise the console would show a new password while the phone
        kept registering with the old one. An empty password generates one.
        """
        extension = str(extension).strip()
        with self._connect() as db:
            row = db.execute("SELECT owner_user_id FROM extensions WHERE extension=?", (extension,)).fetchone()
        if not row or (owner_user_id is not None and row["owner_user_id"] != int(owner_user_id)):
            raise ValueError("Extension not found")
        if password and any(char in password for char in "\r\n;#"):
            raise ValueError("Invalid SIP password")
        secret = password or self.generate_sip_password()
        owner = row["owner_user_id"]
        device = next(
            (item for item in self.list_sip_accounts(owner, include_password=True)
             if str(item.get("extension") or "") == extension and item["active"]),
            None,
        ) if owner is not None else None
        if device:
            number = str(device.get("phone_number") or "")
            if number and not any(item["number"] == number for item in self.list_numbers(owner)):
                number = ""
            with self._connect() as db:
                db.execute(
                    "UPDATE customer_sip_accounts SET sip_password_enc=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (self.encrypt(secret), device["id"]),
                )
            source = "device"
        else:
            with self._connect() as db:
                db.execute(
                    "UPDATE extensions SET sip_password_enc=?,updated_at=CURRENT_TIMESTAMP WHERE extension=?",
                    (self.encrypt(secret), extension),
                )
            source = "extension"
        return {"password": secret, "source": source, "owner_user_id": owner}

    def delete_sip_account(self, account_id: int):
        with self._connect() as db:
            cursor = db.execute("DELETE FROM customer_sip_accounts WHERE id=?", (int(account_id),))
            if cursor.rowcount == 0:
                raise ValueError("SIP account not found")
            db.commit()

    def list_call_routes(self, owner_user_id: int | None = None):
        import json
        with self._connect() as db:
            where = " WHERE owner_user_id=?" if owner_user_id is not None else ""
            params = (int(owner_user_id),) if owner_user_id is not None else ()
            rows = db.execute(f"SELECT id,owner_user_id,phone_number,name,route_json,active,created_at,updated_at FROM call_routes{where} ORDER BY name", params).fetchall()
        result = []
        for row in rows:
            item = dict(row); item["route"] = json.loads(item.pop("route_json")); result.append(item)
        return result

    ROUTE_NODE_TYPES = {
        "incoming", "simultaneous", "sequential", "ring_group", "business_hours",
        "after_hours", "extension", "voicemail", "forward",
    }

    def _validate_route_nodes(self, route: dict, owner_user_id: int, target_type: str = "number") -> None:
        """One validator for every kind of call flow.

        Ring destinations must be extensions the customer owns, and a step that
        names a saved group must belong to that customer with members drawn from
        the group, so a flow can never ring somebody else's phone.
        """
        if not isinstance(route, dict) or not isinstance(route.get("nodes"), list) or len(route["nodes"]) > 100:
            raise ValueError("Call route must contain a nodes array with at most 100 nodes")
        if any(not isinstance(node, dict) or node.get("type") not in self.ROUTE_NODE_TYPES for node in route["nodes"]):
            raise ValueError("Call route contains an unsupported node")
        owned_extensions = {row["extension"] for row in self.list_extensions(int(owner_user_id))}
        owned_groups = {str(row["id"]): set(row["members"]) for row in self.list_groups(int(owner_user_id))}
        for node in route["nodes"]:
            node_type = node["type"]
            if node_type in {"simultaneous", "sequential", "ring_group"}:
                destinations = node.get("extensions")
                if not isinstance(destinations, list) or not destinations or any(str(ext) not in owned_extensions for ext in destinations):
                    raise ValueError("Ring destinations must be extensions owned by this customer")
                if not 5 <= int(node.get("timeout", 0)) <= 120:
                    raise ValueError("Ring timeout must be between 5 and 120 seconds")
                group_id = str(node.get("group_id") or "")
                if group_id:
                    if group_id not in owned_groups:
                        raise ValueError("Ring group is not owned by this customer")
                    if any(str(ext) not in owned_groups[group_id] for ext in destinations):
                        raise ValueError("Ring destinations must come from the selected group")
            elif node_type == "extension" and str(node.get("extension", "")) not in owned_extensions:
                raise ValueError("Route extension is not owned by this customer")
            elif node_type == "voicemail" and str(node.get("mailbox", "")) not in owned_extensions:
                raise ValueError("Voicemail mailbox is not owned by this customer")
            elif node_type == "forward":
                if not re.fullmatch(r"\+[1-9][0-9]{7,14}", str(node.get("phone", ""))):
                    raise ValueError("Forwarding destination must use E.164 format")
                if not 5 <= int(node.get("timeout", 0)) <= 120:
                    raise ValueError("Forward timeout must be between 5 and 120 seconds")
            elif node_type == "business_hours":
                if not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", str(node.get("start", ""))) or not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", str(node.get("end", ""))):
                    raise ValueError("Business hours must include valid opening and closing times")
                days = node.get("days")
                if not isinstance(days, list) or not days or any(int(day) not in range(1, 8) for day in days):
                    raise ValueError("Business hours must include valid weekdays")

    def list_groups(self, owner_user_id: int | None = None):
        with self._connect() as db:
            where = " WHERE owner_user_id=?" if owner_user_id is not None else ""
            rows = db.execute(
                f"SELECT id,owner_user_id,name,members,timeout,active,created_at,updated_at FROM extension_groups{where} ORDER BY name",
                (int(owner_user_id),) if owner_user_id is not None else (),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["members"] = [ext for ext in str(item["members"] or "").split(",") if ext]
            result.append(item)
        return result

    def get_group(self, group_id, owner_user_id: int | None = None):
        return next(
            (group for group in self.list_groups(owner_user_id) if str(group["id"]) == str(group_id)),
            None,
        )

    def save_group(self, data: dict, owner_user_id: int):
        """A group is a named set of the customer's extensions: ring them together,
        and give the group its own call flow."""
        owner_user_id = int(owner_user_id)
        group_id = int(data["id"]) if str(data.get("id", "")).isdigit() else None
        name = str(data.get("name", "")).strip()
        if not 2 <= len(name) <= 80 or any(char in name for char in "\r\n;"):
            raise ValueError("Group name must be 2 to 80 characters")
        owned = {row["extension"] for row in self.list_extensions(owner_user_id)}
        members = [str(ext).strip() for ext in (data.get("members") or [])]
        members = [ext for index, ext in enumerate(members) if ext and ext not in members[:index]]
        if any(ext not in owned for ext in members):
            raise ValueError("Group members must be extensions owned by this customer")
        try:
            timeout = min(120, max(5, int(data.get("timeout", 25))))
        except (TypeError, ValueError) as exc:
            raise ValueError("Ring timeout must be between 5 and 120 seconds") from exc
        with self._connect() as db:
            if db.execute(
                "SELECT id FROM extension_groups WHERE owner_user_id=? AND name=? AND id IS NOT ?", (owner_user_id, name, group_id)
            ).fetchone():
                raise ValueError("A group with this name already exists")
            if group_id:
                existing = db.execute("SELECT id FROM extension_groups WHERE id=? AND owner_user_id=?", (group_id, owner_user_id)).fetchone()
                if not existing:
                    raise ValueError("Group not found")
                db.execute(
                    "UPDATE extension_groups SET name=?,members=?,timeout=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (name, ",".join(members), timeout, int(bool(data.get("active", True))), group_id),
                )
                return group_id
            return db.execute(
                "INSERT INTO extension_groups(owner_user_id,name,members,timeout,active) VALUES(?,?,?,?,?)",
                (owner_user_id, name, ",".join(members), timeout, int(bool(data.get("active", True)))),
            ).lastrowid

    def delete_group(self, group_id, owner_user_id: int | None = None):
        """Deleting a group also removes the flow that was written for it; the
        member extensions themselves are untouched."""
        with self._connect() as db:
            where = " WHERE id=?" + (" AND owner_user_id=?" if owner_user_id is not None else "")
            params = (int(group_id), int(owner_user_id)) if owner_user_id is not None else (int(group_id),)
            row = db.execute(f"SELECT owner_user_id FROM extension_groups{where}", params).fetchone()
            if not row:
                raise ValueError("Group not found")
            db.execute("DELETE FROM routing_flows WHERE owner_user_id=? AND target_type='group' AND target=?", (row["owner_user_id"], str(group_id)))
            db.execute("DELETE FROM extension_groups WHERE id=?", (int(group_id),))

    def inbound_plan(self, number: str, fallback_extension: str = "") -> dict:
        """Decide what rings when a DID is called, and what happens if nobody answers.

        This is the one place the stored flow is turned into a call plan, so the
        engine and the builder can never drift apart. A number without a stored
        flow keeps the legacy behaviour: ring its extension, and honour that
        extension's voicemail switch.
        """
        number = str(number or "").strip()
        row = next((item for item in self.list_numbers() if item["number"] == number), None)
        extension = str((row or {}).get("inbound_extension") or fallback_extension or "").strip()
        owner = (row or {}).get("owner_user_id")
        flow = None
        if owner is not None:
            flow = next((item for item in self.list_call_routes(int(owner)) if item["phone_number"] == number), None)
        if not flow:
            mailbox = extension if extension and any(
                item["extension"] == extension and item["voicemail_enabled"] for item in self.list_extensions(int(owner) if owner else None)
            ) else ""
            return {"destinations": [extension] if extension else [], "timeout": 30, "voicemail": mailbox, "forward": "", "outside_hours": False}
        nodes = (flow.get("route") or {}).get("nodes") or []
        allowed = {
            item["extension"] for item in self.list_extensions(int(owner))
            if item["active"]
        } if owner is not None else set()
        groups = {str(item["id"]): item for item in self.list_groups(int(owner))} if owner is not None else {}
        now = datetime.now()
        open_now, seen_hours = False, False
        destinations, timeout, voicemail, forward = [], 30, "", ""
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = str(node.get("type") or "")
            if kind == "business_hours" and not seen_hours:
                seen_hours = True
                days = [int(day) for day in (node.get("days") or []) if str(day).isdigit()]
                start, end = str(node.get("start") or "09:00"), str(node.get("end") or "17:00")
                weekday = now.isoweekday()
                clock = now.strftime("%H:%M")
                open_now = (not days or weekday in days) and start <= clock <= end
                continue
            if seen_hours and not open_now:
                # Outside business hours only a terminal step applies; ringing
                # steps are skipped so the caller is not sent to an empty office.
                if kind == "voicemail" and not voicemail:
                    voicemail = str(node.get("mailbox") or "")
                continue
            if kind in {"ring_group", "simultaneous", "sequential"}:
                members = [str(value) for value in (node.get("extensions") or [])]
                group = groups.get(str(node.get("group_id") or ""))
                if group and not members:
                    members = [str(value) for value in (group.get("members") or [])]
                destinations.extend(value for value in members if not allowed or value in allowed)
                if str(node.get("timeout") or "").isdigit():
                    timeout = max(5, min(120, int(node["timeout"])))
            elif kind == "extension":
                value = str(node.get("extension") or "")
                if value and (not allowed or value in allowed):
                    destinations.append(value)
            elif kind == "voicemail" and not voicemail:
                voicemail = str(node.get("mailbox") or "")
            elif kind == "forward" and not forward:
                forward = str(node.get("phone") or "")
        # A ring step may repeat an extension; ring each device once, in order.
        unique = list(dict.fromkeys(destinations))
        if not unique and not voicemail and not forward and extension:
            unique = [extension] if not allowed or extension in allowed else []
        return {
            "destinations": unique, "timeout": timeout, "voicemail": voicemail,
            "forward": forward, "outside_hours": bool(seen_hours and not open_now),
        }

    @staticmethod
    def default_number_route(destinations, voicemail: str = "") -> dict:
        """The flow a number starts with: ring the device(s), and if nobody picks
        up the call simply ends. Expressed with the same node shapes the routing
        canvas edits, so the customer can extend it later (hours, voicemail, ...)."""
        members = [str(ext) for ext in (destinations if isinstance(destinations, (list, tuple, set)) else [destinations])]
        if not members:
            return {"nodes": []}
        label = f"Ring extension {members[0]} for 25s" if len(members) == 1 else f"Ring {len(members)} devices for 25s"
        nodes = [{
            "type": "ring_group", "extensions": members, "timeout": 25,
            "label": label, "configured": True,
        }]
        if voicemail:
            nodes.append({"type": "voicemail", "mailbox": str(voicemail), "label": f"Voicemail {voicemail}", "configured": True})
        return {"nodes": nodes}

    @staticmethod
    def default_extension_route(extension: str, voicemail: bool = False) -> dict:
        """The flow a new extension starts with: ring that extension, and if nobody
        picks up the call ends (voicemail is added only when it is switched on)."""
        extension = str(extension)
        nodes = [{"type": "extension", "extension": extension, "label": f"Ring extension {extension}", "configured": True}]
        if voicemail:
            nodes.append({"type": "voicemail", "mailbox": extension, "label": f"Voicemail {extension}", "configured": True})
        return {"nodes": nodes}

    def extension_voicemail_enabled(self, extension: str) -> bool:
        return any(row["extension"] == str(extension) and row.get("voicemail_enabled") for row in self.list_extensions())

    def ensure_extension_flow(self, owner_user_id: int, extension: str, voicemail: bool = False) -> bool:
        """Write the default flow for an extension that does not have one yet."""
        owner_user_id, extension = int(owner_user_id), str(extension)
        if any(
            flow["target_type"] == "extension" and flow["target"] == extension
            for flow in self.list_routing_flows(owner_user_id, target_type="extension")
        ):
            return False
        self.save_routing_flow(
            owner_user_id,
            {"name": "Extension call flow", "route": self.default_extension_route(extension, voicemail), "active": True},
            target_type="extension", target=extension,
        )
        return True

    def provision_number(self, owner_user_id: int, number: str, description: str = "", actor_user_id: int | None = None) -> dict:
        """Give a newly assigned number everything it needs to work.

        One call creates the extension, its SIP credentials, the DID link (so
        inbound calls actually ring), the default caller ID and both default call
        flows: one for the number and one for the extension. Every piece then
        stays editable - nothing here is a one-way door.
        """
        owner_user_id, number = int(owner_user_id), str(number).strip()
        with self._connect() as db:
            customer = db.execute(
                "SELECT id,username,company_name FROM admin_users WHERE id=? AND role='user' AND active=1", (owner_user_id,)
            ).fetchone()
        if not customer:
            raise ValueError("Customer account is not active")
        assigned = next((row for row in self.list_numbers(owner_user_id) if row["number"] == number), None)
        if not assigned:
            raise ValueError("Assign the number to this customer before provisioning it")
        if assigned.get("inbound_extension"):
            raise ValueError("This number already has an inbound extension")

        extension = self.next_extension_number()
        display_name = (str(description).strip() or assigned.get("description") or f"{customer['company_name'] or customer['username']} main line")[:120]
        password = self.generate_sip_password()
        username = self.generate_sip_username(extension)
        self.save_extension({
            "extension": extension, "display_name": display_name, "sip_username": username, "sip_password": password,
            # Recording is the customer's opt-in per device, so a new line starts
            # with its own switch off; the platform switch is the administrator's
            # veto on top of that, never the reason a device records.
            "active": True, "recording_enabled": False,
        }, owner_user_id)

        # Link the DID to the new extension, carrying the existing billing and
        # carrier fields through untouched.
        self.save_number({
            "number": number, "provider": assigned.get("provider", ""),
            "description": assigned.get("description") or display_name,
            "inbound_extension": extension, "default_outbound": False, "active": True,
            "owner_user_id": owner_user_id,
            "monthly_price": (assigned.get("monthly_price_cents") or 500) / 100,
            "billing_cycle_day": assigned.get("billing_cycle_day") or 1,
            "billing_start": assigned.get("billing_start", ""), "discontinue_at": assigned.get("discontinue_at", ""),
        })
        device = next((item for item in self.list_sip_accounts(owner_user_id) if str(item.get("extension") or "") == extension and item["active"]), None)
        has_default = False
        with self._connect() as db:
            has_default = bool(db.execute(
                "SELECT 1 FROM phone_numbers WHERE inbound_extension=? AND default_outbound=1 AND active=1", (extension,)
            ).fetchone())
        if not has_default:
            self.set_default_outbound_number(extension, number)

        # The line rings the device it was provisioned for. A customer's main line
        # additionally picks up every device they add later (sync_primary_flows),
        # while a number tied to one extension keeps ringing only that extension.
        # Nothing else is added: no answer means the call ends, which is the
        # default the customer starts from and can extend in the builder.
        self.save_call_route(owner_user_id, {
            "phone_number": number, "name": "Main call flow" if self.primary_number(owner_user_id) in ("", number) else "Number call flow",
            "route": self.default_number_route([extension]), "active": True,
        })
        self.ensure_extension_flow(owner_user_id, extension, voicemail=self.extension_voicemail_enabled(extension))

        self.add_activity(
            owner_user_id, actor_user_id or owner_user_id, "number.provisioned", "phone_number", number,
            f"Number {number} assigned with extension {extension}, SIP credentials and default call flows",
        )
        return {
            "number": number,
            "extension": extension,
            "display_name": display_name,
            "sip_username": next(
                (str(row["sip_username"]) for row in self.list_extensions(owner_user_id) if str(row["extension"]) == extension),
                username,
            ),
            "sip_password": password,
            "device_linked": bool(device),
            "default_outbound": not has_default,
            "voicemail": self.extension_voicemail_enabled(extension),
            "flows": ["number", "extension"],
        }

    def list_routing_flows(self, owner_user_id: int | None = None, target_type: str | None = None):
        """Call flows written for one extension or one group.

        Number flows live in `call_routes`: that table carries a legacy UNIQUE
        constraint on the number column, so a second kind of target gets its own
        table with the key it actually needs.
        """
        import json
        clauses, params = [], []
        if owner_user_id is not None:
            clauses.append("owner_user_id=?"); params.append(int(owner_user_id))
        if target_type:
            clauses.append("target_type=?"); params.append(str(target_type))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id,owner_user_id,target_type,target,name,route_json,active,created_at,updated_at FROM routing_flows{where} ORDER BY target_type,target",
                tuple(params),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row); item["route"] = json.loads(item.pop("route_json")); result.append(item)
        return result

    def save_routing_flow(self, owner_user_id: int, data: dict, target_type: str | None = None, target: str | None = None):
        import json
        owner_user_id = int(owner_user_id)
        target_type = str(target_type or data.get("target_type") or "").strip()
        target = str(target if target is not None else data.get("target") or "").strip()
        if target_type not in {"extension", "group"}:
            raise ValueError("Call flow target must be an extension or a group")
        if target_type == "extension":
            if not any(row["extension"] == target for row in self.list_extensions(owner_user_id)):
                raise ValueError("Extension is not owned by this customer")
        else:
            group = self.get_group(target, owner_user_id)
            if not group:
                raise ValueError("Group not found")
            target = str(group["id"])
        route = data.get("route")
        self._validate_route_nodes(route, owner_user_id, target_type=target_type)
        payload = json.dumps(route, separators=(",", ":"))
        default_name = "Extension call flow" if target_type == "extension" else "Group call flow"
        with self._connect() as db:
            existing = db.execute(
                "SELECT id FROM routing_flows WHERE owner_user_id=? AND target_type=? AND target=?", (owner_user_id, target_type, target)
            ).fetchone()
            values = (str(data.get("name") or default_name)[:120], payload, int(bool(data.get("active", True))))
            if existing:
                db.execute("UPDATE routing_flows SET name=?,route_json=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (*values, existing["id"]))
                return existing["id"]
            return db.execute(
                "INSERT INTO routing_flows(owner_user_id,target_type,target,name,route_json,active) VALUES(?,?,?,?,?,?)",
                (owner_user_id, target_type, target, *values),
            ).lastrowid

    def delete_routing_flow(self, flow_id, owner_user_id: int | None = None):
        with self._connect() as db:
            where = " WHERE id=?" + (" AND owner_user_id=?" if owner_user_id is not None else "")
            params = (int(flow_id), int(owner_user_id)) if owner_user_id is not None else (int(flow_id),)
            cursor = db.execute(f"DELETE FROM routing_flows{where}", params)
            if cursor.rowcount == 0:
                raise ValueError("Call flow not found")

    def save_call_route(self, owner_user_id: int, data: dict):
        import json
        phone_number = str(data.get("phone_number", "")).strip()
        if not any(row["number"] == phone_number for row in self.list_numbers(int(owner_user_id))):
            raise ValueError("Phone number is not assigned to this customer")
        route = data.get("route")
        self._validate_route_nodes(route, int(owner_user_id), target_type="number")
        payload = json.dumps(route, separators=(",", ":"))
        with self._connect() as db:
            existing = db.execute("SELECT id,owner_user_id FROM call_routes WHERE phone_number=?", (phone_number,)).fetchone()
            if existing and existing["owner_user_id"] != int(owner_user_id):
                raise ValueError("Call route belongs to another customer")
            if existing:
                db.execute("UPDATE call_routes SET name=?,route_json=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(data.get("name", "Main call flow"))[:120], payload, int(bool(data.get("active", True))), existing["id"]))
                return existing["id"]
            return db.execute("INSERT INTO call_routes(owner_user_id,phone_number,name,route_json,active) VALUES(?,?,?,?,?)", (int(owner_user_id), phone_number, str(data.get("name", "Main call flow"))[:120], payload, int(bool(data.get("active", True))))).lastrowid

    def list_providers(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,name,server,port,username,transport,codecs,allowed_ips,active FROM sip_providers ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_provider_details(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,name,server,port,username,password_enc,transport,codecs,allowed_ips,active FROM sip_providers ORDER BY name"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["password"] = self.decrypt(item.pop("password_enc"))
            result.append(item)
        return result

    def get_provider(self, name: str | None = None):
        with self._connect() as db:
            if name:
                row = db.execute(
                    "SELECT id,name,server,port,username,password_enc,transport,codecs,allowed_ips,active FROM sip_providers WHERE name=? AND active=1",
                    (name,),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT id,name,server,port,username,password_enc,transport,codecs,allowed_ips,active FROM sip_providers WHERE active=1 ORDER BY id LIMIT 1"
                ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["password"] = self.decrypt(item.pop("password_enc"))
        return item

    def first_active_number_for_provider(self, provider: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT number FROM phone_numbers WHERE provider=? AND active=1 ORDER BY id LIMIT 1", (provider,)
            ).fetchone()
        return row["number"] if row else None

    @staticmethod
    def _validate_allowed_ips(value: str) -> str:
        entries = [item.strip() for item in str(value or "").split(",") if item.strip()]
        if len(entries) > 64:
            raise ValueError("Too many provider IP/CIDR entries")
        normalized = []
        for item in entries:
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError(f"Invalid provider IP/CIDR: {item}") from exc
            normalized.append(str(network))
        return ",".join(dict.fromkeys(normalized))

    def save_provider(self, data):
        name = self._validate_config_value(data.get("name", ""), "provider name", 80)
        server = self._validate_provider_server(data.get("server", ""))
        password = str(data.get("password") or "")
        if any(char in password for char in "\r\n;#"):
            raise ValueError("Invalid provider password")
        transport = str(data.get("transport", "udp")).lower().strip()
        if transport not in {"udp", "tcp"}:
            raise ValueError("Provider transport must be udp or tcp")
        try:
            port = int(data.get("port", 5060))
        except (TypeError, ValueError) as exc:
            raise ValueError("Provider port is invalid") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Provider port is invalid")
        codecs = str(data.get("codecs", "ulaw,alaw")).strip()[:120]
        allowed_ips = self._validate_allowed_ips(data.get("allowed_ips", ""))
        if not allowed_ips:
            raise ValueError("At least one provider IP/CIDR allowlist entry is required for inbound SIP")
        username = self._validate_config_value(data.get("username", ""), "provider username", 120)
        with self._connect() as db:
            existing = db.execute("SELECT password_enc FROM sip_providers WHERE name=?", (name,)).fetchone()
            encrypted = self.encrypt(password) if password else (existing["password_enc"] if existing else "")
            if not encrypted:
                raise ValueError("SIP provider password is required for a new provider")
            db.execute(
                """INSERT INTO sip_providers(name,server,port,username,password_enc,transport,codecs,allowed_ips,active) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET server=excluded.server,port=excluded.port,username=excluded.username,password_enc=excluded.password_enc,
                transport=excluded.transport,codecs=excluded.codecs,allowed_ips=excluded.allowed_ips,active=excluded.active,updated_at=CURRENT_TIMESTAMP""",
                (
                    name,
                    server,
                    port,
                    username,
                    encrypted,
                    transport,
                    codecs,
                    allowed_ips,
                    int(bool(data.get("active", True))),
                ),
            )
        return name

    def list_webhooks(self, include_tokens: bool = False, owner_user_id: int | None = None):
        with self._connect() as db:
            where = " WHERE owner_user_id=?" if owner_user_id is not None else ""
            rows = db.execute(
                f"SELECT id,name,url,token_enc,events,active,created_at,updated_at,owner_user_id FROM webhook_endpoints{where} ORDER BY name",
                (int(owner_user_id),) if owner_user_id is not None else (),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            encrypted = item.pop("token_enc")
            item["has_token"] = bool(encrypted)
            if include_tokens:
                item["token"] = self.decrypt(encrypted) if encrypted else ""
            result.append(item)
        return result

    def save_webhook(self, data, owner_user_id: int | None = None):
        from urllib.parse import urlparse

        name = self._validate_config_value(data.get("name", ""), "webhook name", 80)
        url = str(data.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or len(url) > 1000:
            raise ValueError("Webhook URL must be a valid HTTP or HTTPS URL without embedded credentials")
        events = str(data.get("events", "*")).strip() or "*"
        event_items = [item.strip() for item in events.split(",") if item.strip()]
        if len(event_items) > 50 or any(not re.fullmatch(r"(?:\*|call\.[a-z_]+)", item) for item in event_items):
            raise ValueError("Events must be * or comma-separated call event names")
        events = ",".join(dict.fromkeys(event_items))
        token = str(data.get("token") or "")
        if len(token) > 1000 or any(char in token for char in "\r\n"):
            raise ValueError("Invalid webhook token")
        webhook_id = data.get("id")
        with self._connect() as db:
            existing = None
            if webhook_id not in {None, ""}:
                existing = db.execute("SELECT id,token_enc,owner_user_id FROM webhook_endpoints WHERE id=?", (int(webhook_id),)).fetchone()
                if existing and owner_user_id is not None and existing["owner_user_id"] != int(owner_user_id):
                    raise ValueError("Webhook not found")
            if existing is None:
                existing = db.execute("SELECT id,token_enc,owner_user_id FROM webhook_endpoints WHERE name=? AND (owner_user_id=? OR (owner_user_id IS NULL AND ? IS NULL))", (name, owner_user_id, owner_user_id)).fetchone()
            encrypted = self.encrypt(token) if token else (existing["token_enc"] if existing else "")
            conflict = db.execute("SELECT id FROM webhook_endpoints WHERE name=?", (name,)).fetchone()
            if conflict and (not existing or conflict["id"] != existing["id"]):
                raise ValueError("A webhook with this name already exists")
            if existing:
                db.execute(
                    "UPDATE webhook_endpoints SET name=?,url=?,token_enc=?,events=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (name, url, encrypted, events, int(bool(data.get("active", True))), existing["id"]),
                )
                return existing["id"]
            cursor = db.execute(
                "INSERT INTO webhook_endpoints(name,url,token_enc,events,active,owner_user_id) VALUES(?,?,?,?,?,?)",
                (name, url, encrypted, events, int(bool(data.get("active", True))), owner_user_id),
            )
            return cursor.lastrowid

    def enqueue_webhook(self, endpoint_id: int, event: str, payload: dict):
        import json
        delivery_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO webhook_deliveries(id,endpoint_id,event,payload) VALUES(?,?,?,?)",
                       (delivery_id, int(endpoint_id), event, json.dumps(payload, separators=(",", ":"), sort_keys=True)))
        return delivery_id

    def pending_webhook_deliveries(self, limit: int = 100):
        import json
        with self._connect() as db:
            db.execute("DELETE FROM webhook_deliveries WHERE (status='delivered' OR attempts>=5) AND updated_at<datetime('now','-30 days')")
            rows = db.execute("SELECT id,endpoint_id,event,payload,attempts FROM webhook_deliveries WHERE status IN ('pending','failed') AND attempts<5 AND next_attempt_at<=CURRENT_TIMESTAMP ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        result = []
        endpoints = {row["id"]: row for row in self.list_webhooks(include_tokens=True)}
        for row in rows:
            endpoint = endpoints.get(row["endpoint_id"])
            if endpoint and endpoint["active"]:
                item = dict(row); item["payload"] = json.loads(item["payload"]); item["endpoint"] = endpoint; result.append(item)
        return result

    def finish_webhook_delivery(self, delivery_id: str, success: bool, error: str | None):
        with self._connect() as db:
            db.execute("""UPDATE webhook_deliveries SET status=?,attempts=attempts+1,last_error=?,
                delivered_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE delivered_at END,
                next_attempt_at=datetime('now','+' || MIN(300,30*(attempts+1)) || ' seconds'),updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                ("delivered" if success else "failed", error, int(success), delivery_id))

    def list_webhook_deliveries(self, limit: int = 50, owner_user_id: int | None = None):
        with self._connect() as db:
            if owner_user_id is None:
                rows = db.execute("SELECT id,endpoint_id,event,status,attempts,last_error,delivered_at,created_at,updated_at FROM webhook_deliveries ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = db.execute("SELECT d.id,d.endpoint_id,d.event,d.status,d.attempts,d.last_error,d.delivered_at,d.created_at,d.updated_at FROM webhook_deliveries d JOIN webhook_endpoints e ON e.id=d.endpoint_id WHERE e.owner_user_id=? ORDER BY d.created_at DESC LIMIT ?", (int(owner_user_id), limit)).fetchall()
        return [dict(row) for row in rows]

    def list_api_keys(self, owner_user_id: int | None = None):
        with self._connect() as db:
            where = " WHERE owner_user_id=?" if owner_user_id is not None else ""
            rows = db.execute(f"SELECT id,name,prefix,scopes,active,last_used_at,created_at,owner_user_id FROM api_keys{where} ORDER BY name", (int(owner_user_id),) if owner_user_id is not None else ()).fetchall()
        return [dict(row) for row in rows]

    def create_api_key(self, name: str, scopes: str = "*", owner_user_id: int | None = None):
        name = self._validate_config_value(name, "API key name", 80)
        allowed = {"*", "calls:read", "calls:write", "recordings:read", "voicemail:read", "voicemail:write", "config:read", "webhooks:manage"}
        items = list(dict.fromkeys(item.strip() for item in str(scopes).split(",") if item.strip()))
        if not items or any(item not in allowed for item in items) or ("*" in items and len(items) != 1):
            raise ValueError("Invalid API key scopes")
        token = "eip_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            cursor = db.execute("INSERT INTO api_keys(name,prefix,key_hash,scopes,owner_user_id) VALUES(?,?,?,?,?)", (name, token[:12], digest, ",".join(items), owner_user_id))
        return cursor.lastrowid, token

    def update_api_key(self, key_id: int, data: dict, owner_user_id: int | None = None):
        """Rename a key or narrow its scopes. The secret is a hash and is never
        rewritten: to change what a key can do, this edits the grant, and to stop
        it, the key is revoked."""
        name = self._validate_config_value(str(data.get("name", "")).strip(), "API key name", 80)
        allowed = {"*", "calls:read", "calls:write", "recordings:read", "voicemail:read", "voicemail:write", "config:read", "webhooks:manage"}
        items = list(dict.fromkeys(item.strip() for item in str(data.get("scopes", "*")).split(",") if item.strip()))
        if not items or any(item not in allowed for item in items) or ("*" in items and len(items) != 1):
            raise ValueError("Invalid API key scopes")
        with self._connect() as db:
            where = " WHERE id=?" + (" AND owner_user_id=?" if owner_user_id is not None else "")
            params = (int(key_id), int(owner_user_id)) if owner_user_id is not None else (int(key_id),)
            cursor = db.execute(f"UPDATE api_keys SET name=?,scopes=?{where}", (name, ",".join(items), *params))
            if cursor.rowcount == 0:
                raise ValueError("API key not found")

    def revoke_api_key(self, key_id: int, owner_user_id: int | None = None):
        with self._connect() as db:
            if owner_user_id is None:
                result = db.execute("DELETE FROM api_keys WHERE id=?", (int(key_id),))
            else:
                result = db.execute("DELETE FROM api_keys WHERE id=? AND owner_user_id=?", (int(key_id), int(owner_user_id)))
            if result.rowcount == 0:
                raise ValueError("API key not found")

    def authenticate_api_key(self, token: str, required_scope: str | None = None):
        if not token.startswith("eip_") or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            row = db.execute("SELECT id,name,scopes,owner_user_id FROM api_keys WHERE key_hash=? AND active=1", (digest,)).fetchone()
            if not row:
                return None
            scopes = set(row["scopes"].split(","))
            if required_scope and "*" not in scopes and required_scope not in scopes:
                return False
            db.execute("UPDATE api_keys SET last_used_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        return {"id": row["id"], "name": row["name"], "scopes": list(scopes), "owner_user_id": row["owner_user_id"]}

    def claim_idempotency(self, client: str, request_key: str):
        with self._connect() as db:
            db.execute("DELETE FROM api_idempotency WHERE created_at<datetime('now','-24 hours')")
            row = db.execute("SELECT call_id FROM api_idempotency WHERE client=? AND request_key=?", (client, request_key)).fetchone()
            if row:
                return False, row["call_id"]
            try:
                db.execute("INSERT INTO api_idempotency(client,request_key) VALUES(?,?)", (client, request_key))
                return True, None
            except DB_INTEGRITY_ERRORS:
                row = db.execute("SELECT call_id FROM api_idempotency WHERE client=? AND request_key=?", (client, request_key)).fetchone()
                return False, row["call_id"] if row else None

    def finish_idempotency(self, client: str, request_key: str, call_id: str | None):
        with self._connect() as db:
            if call_id:
                db.execute("UPDATE api_idempotency SET call_id=? WHERE client=? AND request_key=?", (call_id, client, request_key))
            else:
                db.execute("DELETE FROM api_idempotency WHERE client=? AND request_key=?", (client, request_key))

    def delete_webhook(self, webhook_id, owner_user_id: int | None = None):
        with self._connect() as db:
            if owner_user_id is not None and not db.execute("SELECT 1 FROM webhook_endpoints WHERE id=? AND owner_user_id=?", (int(webhook_id), int(owner_user_id))).fetchone():
                raise ValueError("Webhook not found")
            db.execute("UPDATE webhook_deliveries SET status='failed',attempts=5,last_error='webhook deleted',updated_at=CURRENT_TIMESTAMP WHERE endpoint_id=? AND status!='delivered'", (int(webhook_id),))
            result = db.execute("DELETE FROM webhook_endpoints WHERE id=?", (int(webhook_id),))
            if result.rowcount == 0:
                raise ValueError("Webhook not found")

    def delete_provider(self, provider_id):
        with self._connect() as db:
            provider = db.execute("SELECT name FROM sip_providers WHERE id=?", (int(provider_id),)).fetchone()
            if not provider:
                raise ValueError("Provider not found")
            if db.execute("SELECT 1 FROM phone_numbers WHERE provider=?", (provider["name"],)).fetchone():
                raise ValueError("Cannot delete a provider used by a phone number")
            db.execute("DELETE FROM sip_providers WHERE id=?", (int(provider_id),))

    def list_users(self):
        with self._connect() as db:
            rows = db.execute("SELECT id,username,email,extension,full_name,company_name,job_role,phone,role,active,created_at,updated_at FROM admin_users ORDER BY username").fetchall()
        return [dict(row) for row in rows]

    def save_user(self, data, current_user_id: int | None = None):
        username = str(data.get("username", "")).strip().lower()
        email = str(data.get("email", "")).strip().lower()
        extension = str(data.get("extension", "")).strip()
        role = str(data.get("role", "user")).strip().lower()
        password = str(data.get("password") or "")
        full_name = str(data.get("full_name", "")).strip()[:120]
        company_name = str(data.get("company_name", "")).strip()[:160]
        job_role = str(data.get("job_role", "")).strip()[:120]
        phone = str(data.get("phone", "")).strip()[:30]
        if phone and not re.fullmatch(r"\+?[0-9 ()-]{7,30}", phone):
            raise ValueError("Phone number is invalid")
        if not re.fullmatch(r"[a-z0-9._-]{3,80}", username):
            raise ValueError("Username must be 3–80 letters, numbers, dots, underscores, or hyphens")
        if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("A valid user email is required")
        if role not in {"admin", "user"}:
            raise ValueError("Role must be admin or user")
        with self._connect() as db:
            if extension and not db.execute("SELECT 1 FROM extensions WHERE extension=? AND active=1", (extension,)).fetchone():
                raise ValueError("User extension must be active")
            user_id = data.get("id")
            existing = db.execute("SELECT id,password_hash FROM admin_users WHERE id=?", (int(user_id),)).fetchone() if user_id else None
            if not existing and len(password) < 14:
                raise ValueError("A new user password must be at least 14 characters")
            if password and not 14 <= len(password) <= 256:
                raise ValueError("Password must be between 14 and 256 characters")
            password_hash = generate_password_hash(password, method="scrypt") if password else existing["password_hash"]
            active = int(bool(data.get("active", True)))
            if existing and current_user_id == existing["id"] and (not active or role != "admin"):
                raise ValueError("You cannot disable or demote your own administrator account")
            if existing:
                prior = db.execute("SELECT role,active FROM admin_users WHERE id=?", (existing["id"],)).fetchone()
                if prior and prior["role"] == "admin" and prior["active"] and (role != "admin" or not active):
                    count = db.execute("SELECT COUNT(*) FROM admin_users WHERE role='admin' AND active=1").fetchone()[0]
                    if count <= 1:
                        raise ValueError("At least one active administrator is required")
            conflict = db.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()
            if conflict and (not existing or conflict["id"] != existing["id"]):
                raise ValueError("Username already exists")
            email_conflict = db.execute("SELECT id FROM admin_users WHERE email=?", (email,)).fetchone()
            if email_conflict and (not existing or email_conflict["id"] != existing["id"]):
                raise ValueError("Email address already has an account")
            if existing:
                db.execute("UPDATE admin_users SET username=?,email=?,extension=?,full_name=?,company_name=?,job_role=?,phone=?,role=?,active=?,password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (username, email, extension, full_name, company_name, job_role, phone, role, active, password_hash, existing["id"]))
                if role == "user" and extension and email:
                    db.execute("UPDATE extensions SET voicemail_email=?,updated_at=CURRENT_TIMESTAMP WHERE extension=?", (email, extension))
                return existing["id"]
            user_id = db.execute("INSERT INTO admin_users(username,email,extension,full_name,company_name,job_role,phone,role,active,password_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (username, email, extension, full_name, company_name, job_role, phone, role, active, password_hash)).lastrowid
            if role == "user" and extension and email:
                db.execute("UPDATE extensions SET voicemail_email=?,updated_at=CURRENT_TIMESTAMP WHERE extension=?", (email, extension))
            return user_id

    def delete_user(self, user_id: int, current_user_id: int):
        if int(user_id) == int(current_user_id):
            raise ValueError("You cannot delete your own account")
        with self._connect() as db:
            account = db.execute("SELECT role FROM admin_users WHERE id=?", (int(user_id),)).fetchone()
            if account and account["role"] == "admin":
                # Administrators manage the platform; the panel offers no way to
                # remove one, and the API refuses it for the same reason.
                raise ValueError("Administrator accounts cannot be deleted")
            if db.execute("SELECT 1 FROM phone_numbers WHERE owner_user_id=?", (int(user_id),)).fetchone():
                raise ValueError("Reassign or discontinue this customer's phone numbers before deleting the account")
            if db.execute("SELECT 1 FROM extensions WHERE owner_user_id=?", (int(user_id),)).fetchone():
                raise ValueError("Delete or reassign this customer's extensions before deleting the account")
            user = db.execute("SELECT role,active FROM admin_users WHERE id=?", (int(user_id),)).fetchone()
            if user and user["role"] == "admin" and user["active"]:
                count = db.execute("SELECT COUNT(*) FROM admin_users WHERE role='admin' AND active=1").fetchone()[0]
                if count <= 1:
                    raise ValueError("At least one active administrator is required")
            result = db.execute("DELETE FROM admin_users WHERE id=?", (int(user_id),))
            if result.rowcount == 0:
                raise ValueError("User not found")

    def update_user_email(self, user_id: int, email: str):
        email = str(email or "").strip().lower()
        if email and (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            raise ValueError("Email is invalid")
        with self._connect() as db:
            db.execute("UPDATE admin_users SET email=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (email, int(user_id)))
            row = db.execute("SELECT extension FROM admin_users WHERE id=?", (int(user_id),)).fetchone()
            if row and row["extension"]:
                db.execute("UPDATE extensions SET voicemail_email=?,updated_at=CURRENT_TIMESTAMP WHERE extension=?", (email, row["extension"]))

    def get_email_config(self, include_key: bool = False):
        with self._connect() as db:
            row = db.execute("SELECT sendgrid_api_key_enc,from_email,from_name,enabled,updated_at FROM email_config WHERE id=1").fetchone()
        result = {"from_email": "", "from_name": "EngineerIP Voicemail", "enabled": False, "has_api_key": False}
        if row:
            result.update({"from_email": row["from_email"], "from_name": row["from_name"], "enabled": bool(row["enabled"]), "has_api_key": bool(row["sendgrid_api_key_enc"]), "updated_at": row["updated_at"]})
            if include_key:
                result["api_key"] = self.decrypt(row["sendgrid_api_key_enc"]) if row["sendgrid_api_key_enc"] else ""
        return result

    def save_email_config(self, data):
        from_email = str(data.get("from_email", "")).strip().lower()
        from_name = str(data.get("from_name", "EIP Telephony Voicemail")).strip()[:120]
        enabled = bool(data.get("enabled"))
        key = str(data.get("api_key") or "")
        if from_email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", from_email):
            raise ValueError("From email is invalid")
        existing = self.get_email_config(include_key=True)
        key = key or existing.get("api_key", "")
        if enabled and (not key or not from_email):
            raise ValueError("SendGrid API key and verified from email are required")
        encrypted = self.encrypt(key) if key else ""
        with self._connect() as db:
            db.execute("INSERT INTO email_config(id,sendgrid_api_key_enc,from_email,from_name,enabled) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET sendgrid_api_key_enc=excluded.sendgrid_api_key_enc,from_email=excluded.from_email,from_name=excluded.from_name,enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP",
                       (encrypted, from_email, from_name, int(enabled)))

    def claim_voicemail_delivery(self, fingerprint: str, mailbox: str, recipient: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT status,attempts FROM voicemail_deliveries WHERE fingerprint=?", (fingerprint,)).fetchone()
            if row and (row["status"] == "delivered" or row["attempts"] >= 5):
                return False
            if row:
                db.execute("UPDATE voicemail_deliveries SET status='pending',attempts=attempts+1,updated_at=CURRENT_TIMESTAMP WHERE fingerprint=?", (fingerprint,))
            else:
                db.execute("INSERT INTO voicemail_deliveries(fingerprint,mailbox,recipient,status,attempts) VALUES(?,?,?,'pending',1)", (fingerprint, mailbox, recipient))
            return True

    def finish_voicemail_delivery(self, fingerprint: str, success: bool, error: str | None = None):
        with self._connect() as db:
            db.execute("UPDATE voicemail_deliveries SET status=?,last_error=?,delivered_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE delivered_at END,updated_at=CURRENT_TIMESTAMP WHERE fingerprint=?",
                       ("delivered" if success else "failed", error, int(success), fingerprint))

    def list_voicemail_deliveries(self, limit: int = 50):
        with self._connect() as db:
            rows = db.execute("SELECT fingerprint,mailbox,recipient,status,attempts,last_error,delivered_at,updated_at FROM voicemail_deliveries ORDER BY updated_at DESC LIMIT ?", (min(100, max(1, limit)),)).fetchall()
        return [dict(row) for row in rows]

    def recording_platform_enabled(self) -> bool:
        """The administrator's recording switch.

        False means nothing records anywhere, whatever a customer set on their
        own extension; True lets each device follow its own switch. An
        unset/unknown value reads as off, which is the privacy default every
        install starts from.
        """
        value = str(self.get_settings().get("recording_enabled", "")).strip().lower()
        return value in {"true", "1", "yes", "on"}

    def get_settings(self):
        with self._connect() as db:
            rows = db.execute("SELECT `key`,value FROM settings ORDER BY `key`").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_settings(self, values):
        allowed = {
            "recording_enabled",
            "recording_format",
            "recording_retention_days",
            "recording_announcement",
            "recording_announcement_media",
            "recording_beep",
            "recording_max_duration_seconds",
            "default_extension",
            "inbound_fallback_extension",
            "webrtc_enabled",
            "service_host",
            "service_sip_port",
        }
        for key, value in values.items():
            if key not in allowed:
                continue
            text = str(value)
            if len(text) > 2000:
                raise ValueError("Setting is too long")
            if key == "recording_format" and text not in {"wav", "wav49", "gsm", "slin16"}:
                raise ValueError("Unsupported recording format")
            if key in {"recording_enabled", "recording_announcement", "recording_beep", "webrtc_enabled"} and text.lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError(f"Invalid value for {key}")
            if key in {"recording_retention_days", "recording_max_duration_seconds"}:
                try:
                    number = int(text)
                except ValueError as exc:
                    raise ValueError(f"Invalid value for {key}") from exc
                if key == "recording_retention_days" and not 1 <= number <= 3650:
                    raise ValueError("Recording retention must be between 1 and 3650 days")
                if key == "recording_max_duration_seconds" and not 0 <= number <= 86400:
                    raise ValueError("Maximum recording duration must be between 0 and 86400 seconds")
            if key == "service_host":
                # An address a device registers with: no scheme, no path, no port
                # - the port has its own setting, and a typo here breaks every
                # phone at once.
                text = text.strip()
                if text and not self.SERVICE_HOST_RE.match(text):
                    raise ValueError("Service address must be a hostname or an IP address, without a scheme, path or port")
            if key == "service_sip_port":
                try:
                    port = int(text)
                except ValueError as exc:
                    raise ValueError("Invalid SIP port") from exc
                if not 1 <= port <= 65535:
                    raise ValueError("SIP port must be between 1 and 65535")
            if key in {"default_extension", "inbound_fallback_extension"}:
                if not text.isdigit() or not 100 <= int(text) <= 999:
                    raise ValueError(f"Invalid extension for {key}")
                with self._connect() as db:
                    if not db.execute("SELECT 1 FROM extensions WHERE extension=? AND active=1", (text,)).fetchone():
                        raise ValueError(f"{key.replace('_', ' ').title()} must be an active extension")
        with self._connect() as db:
            for key, value in values.items():
                if key not in allowed:
                    continue
                # A switch is a switch: store the canonical text, however the
                # caller spelled the boolean, so a reader comparing the stored
                # value and the generated configuration always sees the same one.
                text = str(value)
                if key in {"recording_enabled", "recording_announcement", "recording_beep", "webrtc_enabled"}:
                    text = "true" if text.strip().lower() in {"true", "1", "yes", "on"} else "false"
                if key == "service_host":
                    text = text.strip()
                db.execute(
                    "INSERT INTO settings(`key`,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                    (str(key), text),
                )

    # ------------------------------------------------ per-customer call defaults
    # Where a customer's calls go when nothing more specific is chosen: which
    # extension an API request without one uses, and where a DID that has no
    # valid destination lands. Both are the customer's own decision, and both are
    # per customer because an extension belongs to exactly one customer - a
    # single platform-wide value could only ever be right for one of them.
    CALL_DEFAULTS_KEY = "call_defaults"

    def customer_call_defaults(self, owner_user_id: int) -> dict:
        """The stored choice, or the customer's first active device as a default."""
        owner_user_id = int(owner_user_id)
        try:
            stored = json.loads(self.get_settings().get(self.CALL_DEFAULTS_KEY) or "{}")
        except (TypeError, ValueError):
            stored = {}
        entry = stored.get(str(owner_user_id)) or {}
        active = [row["extension"] for row in self.list_extensions(owner_user_id) if row["active"]]

        def pick(value) -> str:
            value = str(value or "").strip()
            return value if value in active else (active[0] if active else "")

        return {"outbound": pick(entry.get("outbound")), "fallback": pick(entry.get("fallback"))}

    def set_customer_call_defaults(self, owner_user_id: int, outbound: str, fallback: str) -> dict:
        owner_user_id = int(owner_user_id)
        owned = {row["extension"] for row in self.list_extensions(owner_user_id) if row["active"]}
        for label, value in (("Default outbound extension", outbound), ("Inbound fallback extension", fallback)):
            value = str(value or "").strip()
            if value and value not in owned:
                raise ValueError(f"{label} must be one of your active extensions")
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE `key`=?", (self.CALL_DEFAULTS_KEY,)).fetchone()
        try:
            stored = json.loads(row["value"]) if row else {}
        except (TypeError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored[str(owner_user_id)] = {"outbound": str(outbound or "").strip(), "fallback": str(fallback or "").strip()}
        # Written directly rather than through set_settings: this is one JSON
        # document for every customer, and it is not an administrative setting.
        with self._connect() as db:
            db.execute(
                "INSERT INTO settings(`key`,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                (self.CALL_DEFAULTS_KEY, json.dumps(stored, sort_keys=True)),
            )
        return self.customer_call_defaults(owner_user_id)

    def call_default_for(self, field: str, owner_user_id: int | None) -> str:
        """The effective default, falling back to the legacy platform-wide value."""
        if owner_user_id is not None:
            value = self.customer_call_defaults(int(owner_user_id)).get(field, "")
            if value:
                return value
        legacy = str(self.get_settings().get("default_extension" if field == "outbound" else "inbound_fallback_extension", "")).strip()
        if legacy and any(row["extension"] == legacy and row["active"] for row in self.list_extensions()):
            return legacy
        return ""

    def extension_is_a_call_default(self, extension: str) -> bool:
        """Is any customer relying on this extension as their outbound or fallback?"""
        extension = str(extension)
        try:
            stored = json.loads(self.get_settings().get(self.CALL_DEFAULTS_KEY) or "{}")
        except (TypeError, ValueError):
            stored = {}
        if any(str(entry.get(field) or "") == extension for entry in stored.values() if isinstance(entry, dict) for field in ("outbound", "fallback")):
            return True
        with self._connect() as db:
            return bool(db.execute(
                "SELECT 1 FROM settings WHERE `key` IN ('default_extension','inbound_fallback_extension') AND value=?",
                (extension,),
            ).fetchone())

    def decrypt(self, ciphertext):
        from cryptography.fernet import Fernet
        key = base64.urlsafe_b64encode(hashlib.sha256(self.secret_key).digest())
        return Fernet(key).decrypt(ciphertext.encode()).decode()

    def encrypt(self, plaintext):
        from cryptography.fernet import Fernet
        key = base64.urlsafe_b64encode(hashlib.sha256(self.secret_key).digest())
        return Fernet(key).encrypt(plaintext.encode()).decode()


def register_admin(app, config, on_telephony_change=None):
    store = SettingsStore(config.DATABASE_URI or config.SETTINGS_DB_PATH, config.SECRET_KEY)

    def flow_owner(data: dict, target_type: str, target: str, phone_number: str = "") -> int:
        """Whose call flow a request is about.

        A customer session is always that customer. An administrator session
        names the customer through the flow's own target - the number, extension
        or group decides - and falls back to the customer selected in the
        workspace, so a flow can never be written across customers."""
        if session.get("admin_role") != "admin":
            return int(session["admin_user_id"])
        if target_type == "number":
            number = str(phone_number or target or "").strip()
            owner = next((row["owner_user_id"] for row in store.list_numbers() if row["number"] == number), None)
        elif target_type == "extension":
            owner = next((row["owner_user_id"] for row in store.list_extensions() if str(row["extension"]) == str(target)), None)
        else:
            group = store.get_group(target)
            owner = group["owner_user_id"] if group else None
        if owner:
            return int(owner)
        chosen = data.get("owner_user_id")
        if str(chosen or "").strip().isdigit():
            return int(chosen)
        raise ValueError("Choose the customer this call flow belongs to")

    store.ensure_bootstrap_admin(config.ADMIN_USERNAME, config.ADMIN_PASSWORD)
    store.bootstrap_telephony(config)
    app.extensions["settings_store"] = store
    web_dir = str(Path(app.root_path).parent / "web")

    def apply_change():
        if on_telephony_change:
            try:
                on_telephony_change()
            except Exception:
                app.logger.exception("Failed to apply telephony configuration change")

    def login_required(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("admin_user_id"):
                if request.path.startswith("/admin/api/"):
                    return jsonify({"error": "authentication required"}), 401
                return redirect("/login")
            user = store.get_user(int(session["admin_user_id"]))
            if not user or not user["active"]:
                session.clear()
                if request.path.startswith("/admin/api/"):
                    return jsonify({"error": "account disabled"}), 401
                return redirect("/login")
            session["admin_username"], session["admin_email"] = user["username"], user["email"]
            session["admin_extension"], session["admin_role"] = user["extension"], user["role"]
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                token = request.headers.get("X-CSRF-Token", "")
                expected = session.get("csrf_token", "")
                if not expected or not token or not secrets.compare_digest(token, expected):
                    return jsonify({"error": "csrf validation failed"}), 403
            return fn(*args, **kwargs)
        return wrapped

    def admin_required(fn):
        @login_required
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if session.get("admin_role") != "admin":
                return jsonify({"error": "administrator permission required"}), 403
            return fn(*args, **kwargs)
        return wrapped

    def login_rate_limited():
        username = str((request.get_json(silent=True) or request.form).get("username", "")).strip().lower()[:80]
        now = time.monotonic()
        keys = [f"ip:{request.remote_addr or 'unknown'}", f"user:{username}"]
        limited = False
        for key in keys:
            bucket = _LOGIN_BUCKETS[key]
            while bucket and now - bucket[0] >= _LOGIN_WINDOW:
                bucket.popleft()
            if len(bucket) >= _LOGIN_LIMIT:
                limited = True
            else:
                bucket.append(now)
        return limited

    @app.get("/admin-assets/<path:filename>")
    def admin_asset(filename):
        if filename not in {"admin.css", "admin.js"}:
            return jsonify({"error": "not found"}), 404
        return send_from_directory(web_dir, filename, max_age=3600)

    @app.get("/")
    def landing_page(): return send_from_directory(web_dir, "index.html")

    @app.post("/signup")
    def public_signup():
        if login_rate_limited():
            return jsonify({"error": "too many signup attempts; try again later"}), 429
        try:
            data = request.get_json(silent=True) or {}
            required = {"full_name": "Name", "company_name": "Company name", "job_role": "Role", "phone": "Phone number"}
            missing = [label for field, label in required.items() if not str(data.get(field, "")).strip()]
            if missing:
                raise ValueError(f"Required fields: {', '.join(missing)}")
            data.update({"role": "user", "extension": "", "active": True})
            user_id = store.save_user(data)
            store.add_activity(user_id, user_id, "customer.signup", "customer", user_id, f"{data['company_name']} account created")
            store.add_notification(user_id, "welcome", "Welcome to EIP Telephony", "Request a phone number to begin configuring your telephony environment.")
            return jsonify({"ok": True, "user_id": user_id, "message": "Account created. Sign in to continue."}), 201
        except INPUT_DB_ERRORS as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/login")
    @app.get("/admin/login")
    def admin_login_page(): return send_from_directory(web_dir, "admin-login.html")

    @app.get("/documentation")
    @login_required
    def documentation_page():
        """The setup and API reference that the console links to.

        Signed-in only: it describes the integration surface of this deployment,
        so it is not something an anonymous visitor needs to read. The page holds
        no customer data; the only thing rendered into it is the service address
        an administrator configured, so every example names this deployment
        rather than a placeholder.
        """
        page_file = Path(web_dir) / "documentation.html"
        page = page_file.read_text(encoding="utf-8")
        service = store.service_address(request.host.split(":")[0] if request.host else "")
        api_base = service["api_base"] or str(request.host_url).rstrip("/")
        sip_host = service["sip"] or (request.host or "")
        page = page.replace("{{API_BASE}}", api_base).replace("{{SIP_HOST}}", sip_host)
        return Response(page, mimetype="text/html")

    @app.post("/login")
    @app.post("/admin/login")
    def admin_login():
        # This endpoint is consumed by JavaScript and must always return JSON.
        # Redirecting an existing session to /admin makes fetch follow the
        # redirect and then attempt to parse the HTML console as JSON.
        if session.get("admin_user_id"):
            return jsonify({"ok": True, "already_authenticated": True})
        if login_rate_limited():
            return jsonify({"error": "too many login attempts"}), 429
        data = request.get_json(silent=True) or request.form
        try:
            user = store.authenticate(str(data.get("username", "")), str(data.get("password", "")))
        except Exception:
            current_app.logger.exception("Administrator login database lookup failed")
            return jsonify({"error": "Login service unavailable; check the database connection"}), 503
        if not user:
            return jsonify({"error": "invalid username or password"}), 401
        session.clear()
        session["admin_user_id"] = user["id"]
        session["admin_username"] = user["username"]
        session["admin_role"] = user["role"]
        session["admin_email"] = user["email"]
        session["admin_extension"] = user["extension"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        return jsonify({"ok": True})

    @app.post("/admin/logout")
    @login_required
    def admin_logout(): session.clear(); return jsonify({"ok": True})

    @app.get("/admin")
    @login_required
    def admin_page(): return send_from_directory(web_dir, "admin.html")

    @app.get("/admin/api/state")
    @login_required
    def admin_state():
        is_admin = session.get("admin_role") == "admin"
        assigned = str(session.get("admin_extension") or "")
        user_id = int(session["admin_user_id"])
        extensions = store.list_extensions() if is_admin else store.list_extensions(user_id)
        owned_extensions = [row["extension"] for row in extensions]
        if is_admin:
            voicemail_messages = current_app.extensions["voicemail_store"].list_messages()
            visible_numbers = store.list_numbers()
        else:
            voicemail_messages = [message for ext in owned_extensions for message in current_app.extensions["voicemail_store"].list_messages(ext)]
            visible_numbers = store.list_numbers(user_id)
            for number in visible_numbers:
                number.pop("provider", None)
        all_users = store.list_users() if is_admin else []
        all_sip = store.list_sip_accounts(None if is_admin else user_id)
        customer_metrics = []
        if is_admin:
            all_numbers = store.list_numbers()
            all_extensions = store.list_extensions()
            for customer in (row for row in all_users if row["role"] == "user"):
                customer_metrics.append({
                    **customer,
                    "number_count": sum(row.get("owner_user_id") == customer["id"] for row in all_numbers),
                    "sip_count": sum(row.get("owner_user_id") == customer["id"] for row in all_sip),
                    "extension_count": sum(row.get("owner_user_id") == customer["id"] for row in all_extensions),
                    "payment_status": "overdue" if any(row["user_id"] == customer["id"] and row["status"] == "open" and row["due_at"] < date.today().isoformat() for row in store.list_invoices()) else "current",
                })
        return jsonify({
            "username": session.get("admin_username"), "email": session.get("admin_email", ""),
            "assigned_extension": assigned, "role": session.get("admin_role"), "is_admin": is_admin,
            # The console scopes owner-bound records (flows, groups) with it.
            "user_id": user_id,
            "csrf_token": session.get("csrf_token"), "extensions": extensions,
            "phone_numbers": visible_numbers,
            "providers": store.list_providers() if is_admin else [],
            "webhooks": store.list_webhooks(owner_user_id=None if is_admin else user_id),
            "webhook_deliveries": store.list_webhook_deliveries(owner_user_id=None if is_admin else user_id),
            "users": all_users,
            "customers": customer_metrics,
            "sip_accounts": all_sip,
            "requests": store.list_requests(None if is_admin else user_id),
            "pending_request_count": sum(row["status"] == "pending" for row in store.list_requests(None if is_admin else user_id)),
            "activity": store.list_activity(None if is_admin else user_id),
            "notifications": store.list_notifications(user_id),
            "call_routes": store.list_call_routes(None if is_admin else user_id),
            "routing_flows": store.list_routing_flows(None if is_admin else user_id),
            "groups": store.list_groups(None if is_admin else user_id),
            "api_keys": store.list_api_keys(owner_user_id=None if is_admin else user_id),
            "invoices": store.list_invoices(None if is_admin else user_id),
            "email_config": store.get_email_config() if is_admin else {},
            "email_deliveries": store.list_voicemail_deliveries() if is_admin else [],
            "call_summary": current_app.extensions["telephony_service"].store.summary(extensions=owned_extensions if not is_admin else None),
            "voicemail_summary": {
                "total": len(voicemail_messages), "new": sum(row["folder"] == "inbox" for row in voicemail_messages),
                "old": sum(row["folder"] == "old" for row in voicemail_messages), "urgent": sum(row["folder"] == "urgent" for row in voicemail_messages),
            },
            "settings": store.get_settings() if is_admin else {},
            # The platform recording switch, so a customer's own switch can say
            # when it cannot take effect.
            "recording_platform_enabled": store.recording_platform_enabled(),
            # Where customers register and what the API examples are built from.
            # The administrator sees the stored value; everybody else sees the
            # address their own devices should use.
            "service_address": store.service_address(request.host.split(":")[0] if request.host else ""),
            "call_defaults": store.customer_call_defaults(user_id) if not is_admin else {},
        })

    @app.get("/admin/api/call-defaults")
    @login_required
    def admin_call_defaults():
        """Where this account's calls go when nothing more specific is chosen."""
        if session.get("admin_role") == "admin":
            requested = str(request.args.get("customer_id") or "")
            if not requested.isdigit():
                return jsonify({"error": "choose a customer"}), 400
            customer_id = int(requested)
            if not store.get_user(customer_id):
                return jsonify({"error": "customer not found"}), 404
        else:
            customer_id = int(session["admin_user_id"])
        return jsonify({
            "call_defaults": store.customer_call_defaults(customer_id),
            "extensions": [row["extension"] for row in store.list_extensions(customer_id) if row["active"]],
        })

    @app.post("/admin/api/call-defaults")
    @login_required
    def admin_save_call_defaults():
        """The customer decides where their own calls land, not the platform."""
        if session.get("admin_role") == "admin":
            return jsonify({"error": "call defaults belong to the customer"}), 403
        try:
            data = request.get_json(silent=True) or {}
            saved = store.set_customer_call_defaults(
                int(session["admin_user_id"]),
                str(data.get("outbound") or ""), str(data.get("fallback") or ""),
            )
            apply_change()
            return jsonify({"ok": True, "call_defaults": saved})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/system")
    @admin_required
    def admin_system():
        """What the platform is doing right now: calls, load and recording work.

        Everything here is measured, never estimated. Asterisk is asked for its
        live channels and endpoints, the call store for what is in progress, and
        the host for its load and memory. Anything unreachable reports zero
        rather than guessing.
        """
        service = current_app.extensions["telephony_service"]
        calls = service.store.all()
        in_progress = [call for call in calls if call.status in {"initiated", "ringing", "answered", "dialing_customer"}]
        today = datetime.now(timezone.utc).date().isoformat()
        by_start = [call for call in calls if str(call.started_at or "").startswith(today)]

        def moment(value):
            try:
                return datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                return None

        # Peak concurrency today: the most calls overlapping at any instant.
        events = []
        for call in by_start:
            started, ended = moment(call.started_at), moment(call.ended_at)
            if not started:
                continue
            events.append((started, 1))
            events.append((ended or datetime.now(timezone.utc), -1))
        peak, live = 0, 0
        for _, delta in sorted(events, key=lambda item: (item[0], -item[1])):
            live += delta
            peak = max(peak, live)

        try:
            channels = service.asterisk.list_channels() or []
        except Exception:
            channels = []
        try:
            endpoints = [row for row in (service.asterisk.list_endpoints() or [])
                         if str(row.get("technology") or "").lower() == "pjsip"]
        except Exception:
            endpoints = []
        online = sum(1 for row in endpoints if str(row.get("state") or "").lower() in {"online", "available"})

        cpu_count = os.cpu_count() or 1
        try:
            load1, load5, load15 = os.getloadavg()
        except OSError:
            load1 = load5 = load15 = 0.0
        memory_total = memory_available = 0
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        memory_total = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        memory_available = int(line.split()[1]) * 1024
        except OSError:
            pass

        running = [call for call in calls if call.recording_status == "recording"]
        return jsonify({
            "calls": {
                "in_progress": len(in_progress),
                "ringing": sum(1 for call in in_progress if call.status == "ringing"),
                "connected": sum(1 for call in in_progress if call.status == "answered"),
                "peak_today": peak,
                "today": len(by_start),
                "answered_today": sum(1 for call in by_start if call.answered),
            },
            "channels": {"active": len(channels)},
            "devices": {"online": online, "total": len(endpoints)},
            "recordings": {
                "in_progress": len(running),
                "today": sum(1 for call in by_start if call.recording_name),
            },
            "host": {
                "load_1": round(load1, 2), "load_5": round(load5, 2), "load_15": round(load15, 2),
                "cpu_count": cpu_count, "load_pct": min(100, round((load1 / cpu_count) * 100)),
                "memory_total": memory_total, "memory_available": memory_available,
                "memory_pct": round(((memory_total - memory_available) / memory_total) * 100) if memory_total else 0,
            },
        })

    @app.post("/admin/api/extensions")
    @login_required
    def admin_extension():
        try:
            data = request.get_json(silent=True) or {}
            owner = data.get("owner_user_id") if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            owner_id = int(owner) if str(owner or "").isdigit() else None
            existed = any(row["extension"] == str(data.get("extension", "")).strip() for row in store.list_extensions())
            result = store.save_extension(data, owner_id, session.get("admin_role") != "admin")
            credentials = None
            if owner_id:
                store.add_activity(owner_id, int(session["admin_user_id"]), "extension.saved", "extension", result, f"Extension {result} configured")
                # The caller created it, so they get the credentials back once:
                # the console shows them with the provisioning summary.
                if not existed:
                    store.add_activity(
                        owner_id, int(session["admin_user_id"]), "extension.provisioned", "extension", result,
                        f"Extension {result} created with SIP credentials and a default call flow",
                    )
                    credentials = store.reveal_extension_credentials(result, owner_id)
            apply_change()
            return jsonify({"ok": True, "extension": result, "created": not existed, "credentials": credentials})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/extensions/<extension>/password")
    @login_required
    def admin_extension_password(extension):
        """Change the SIP password a device registers with, or generate a new one."""
        try:
            data = request.get_json(silent=True) or {}
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            result = store.set_extension_password(extension, str(data.get("password") or ""), owner)
            if result["owner_user_id"] is not None:
                store.add_activity(
                    result["owner_user_id"], int(session["admin_user_id"]), "extension.password_rotated",
                    "extension", extension, f"SIP password changed for extension {extension}",
                )
            apply_change()
            return jsonify({"ok": True, "sip_password": result["password"], "source": result["source"]})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/extensions/<extension>/credentials")
    @login_required
    def admin_extension_credentials(extension):
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            return jsonify({"credentials": store.reveal_extension_credentials(
                extension, owner, request.host.split(":")[0] if request.host else "",
            )})
        except ValueError as exc: return jsonify({"error": str(exc)}), 404

    @app.get("/admin/api/extensions/next")
    @login_required
    def admin_next_extension():
        """The extension number a new line would get, for the console to prefill."""
        return jsonify({"extension": store.next_extension_number()})

    @app.delete("/admin/api/extensions/<extension>")
    @login_required
    def admin_delete_extension(extension):
        try:
            if current_app.extensions["voicemail_store"].list_messages(extension):
                raise ValueError("Cannot delete an extension that still has voicemail messages")
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            store.delete_extension(extension, owner); apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/numbers")
    @admin_required
    def admin_number():
        try:
            data = request.get_json(silent=True) or {}
            owner = int(data["owner_user_id"]) if str(data.get("owner_user_id", "")).isdigit() else None
            inbound = str(data.get("inbound_extension", "")).strip()
            auto = "auto" in {inbound.lower(), str(data.get("auto_provision", "")).lower()} or bool(data.get("auto_provision"))
            if auto:
                # Assign first, then provision: a carrier or format problem must
                # not leave a half-built line behind.
                data = {**data, "inbound_extension": "", "default_outbound": False}
            result = store.save_number(data)
            provisioned = None
            if auto and owner:
                provisioned = store.provision_number(owner, result, str(data.get("description", "")), int(session["admin_user_id"]))
                store.add_notification(
                    owner, "number", "Phone line ready",
                    f"{result} is live on extension {provisioned['extension']}. SIP credentials and default call flows were created for you.",
                )
            if owner and not provisioned:
                store.add_activity(owner, int(session["admin_user_id"]), "number.assigned", "phone_number", result, f"Number {result} assigned")
                store.add_notification(owner, "number", "Phone number assigned", f"{result} is now available in your account.")
            apply_change(); return jsonify({"ok": True, "number": result, "provisioned": provisioned})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/numbers/<path:number>")
    @admin_required
    def admin_delete_number(number):
        try:
            store.delete_number(number); apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/providers")
    @admin_required
    def admin_provider():
        try:
            result = store.save_provider(request.get_json(silent=True) or {})
            apply_change(); return jsonify({"ok": True, "provider": result})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/providers/<int:provider_id>")
    @admin_required
    def admin_delete_provider(provider_id):
        try:
            store.delete_provider(provider_id); apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/calls")
    @login_required
    def admin_calls():
        extension = request.args.get("extension", "").strip()
        owned_extensions = None
        if session.get("admin_role") != "admin":
            owned_extensions = [row["extension"] for row in store.list_extensions(int(session["admin_user_id"]))]
            if extension and extension not in owned_extensions:
                return jsonify({"error": "extension not found"}), 404
        status = request.args.get("status", "").strip()
        query = request.args.get("q", "").strip()[:100]
        recordings_only = request.args.get("recordings", "false").lower() == "true"
        if extension and (not extension.isdigit() or not 100 <= int(extension) <= 999):
            return jsonify({"error": "invalid extension"}), 400
        if status and not re.fullmatch(r"[a-z_]{1,40}", status):
            return jsonify({"error": "invalid status"}), 400
        try:
            limit = min(100, max(1, int(request.args.get("limit", "50"))))
            offset = max(0, int(request.args.get("offset", "0")))
        except ValueError:
            return jsonify({"error": "invalid pagination"}), 400
        calls, total = current_app.extensions["telephony_service"].store.search(
            extension=extension or None, extensions=owned_extensions if not extension else None,
            status=status or None, recordings_only=recordings_only,
            query=query or None, limit=limit, offset=offset,
        )
        return jsonify({"calls": [call.to_dict() for call in calls], "total": total, "limit": limit, "offset": offset})

    def visible_call_rows():
        calls = current_app.extensions["telephony_service"].store.all()
        if session.get("admin_role") == "admin":
            return calls
        owned = {row["extension"] for row in store.list_extensions(int(session["admin_user_id"]))}
        return [call for call in calls if call.extension in owned]

    @app.get("/admin/api/calls/export.csv")
    @login_required
    def export_calls_csv():
        recordings_only = request.args.get("recordings", "false").lower() == "true"
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Call ID", "Started", "Direction", "Caller / destination", "Assigned number", "Extension", "Status", "Answered", "Duration seconds", "Provider", "Recording status"])
        def csv_safe(value):
            text = str(value or "")
            return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text
        for call in visible_call_rows():
            if recordings_only and not call.recording_name:
                continue
            writer.writerow([csv_safe(value) for value in [call.call_id, call.started_at, call.direction, call.phone, call.caller_id_number, call.extension, call.status, "yes" if call.answered else "no", call.duration_seconds or 0, call.provider or "", call.recording_status or ""]])
        filename = "recordings.csv" if recordings_only else "call-history.csv"
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/admin/api/analytics")
    @login_required
    def call_analytics():
        calls = visible_call_rows()
        cutoff = (date.today() - timedelta(days=13)).isoformat()
        recent = [call for call in calls if str(call.started_at)[:10] >= cutoff]
        answered = sum(bool(call.answered) for call in calls)
        missed = sum(call.direction == "inbound" and not call.answered for call in calls)
        completed_durations = [int(call.duration_seconds or 0) for call in calls if call.answered]
        days = {(date.today() - timedelta(days=offset)).isoformat(): {"total": 0, "answered": 0} for offset in range(13, -1, -1)}
        extensions = {}
        for call in calls:
            bucket = extensions.setdefault(call.extension, {"extension": call.extension, "total": 0, "answered": 0, "duration_seconds": 0})
            bucket["total"] += 1
            bucket["answered"] += int(bool(call.answered))
            bucket["duration_seconds"] += int(call.duration_seconds or 0)
        for call in recent:
            key = str(call.started_at)[:10]
            if key in days:
                days[key]["total"] += 1
                days[key]["answered"] += int(bool(call.answered))
        return jsonify({
            "total": len(calls), "answered": answered, "missed": missed,
            "answer_rate": round(answered * 100 / len(calls), 1) if calls else 0,
            "average_duration_seconds": round(sum(completed_durations) / len(completed_durations)) if completed_durations else 0,
            "inbound": sum(call.direction == "inbound" for call in calls),
            "outbound": sum(call.direction == "outbound" for call in calls),
            "daily": [{"date": key, **value} for key, value in days.items()],
            "extensions": sorted(extensions.values(), key=lambda row: row["total"], reverse=True)[:10],
        })

    def mailbox_allowed(mailbox: str) -> bool:
        return session.get("admin_role") == "admin" or any(
            row["extension"] == str(mailbox) for row in store.list_extensions(int(session["admin_user_id"]))
        )

    @app.post("/admin/api/calls")
    @login_required
    def admin_start_call():
        # Placing a call is the customer's own action with their own line: an
        # administrator manages accounts and never dials on somebody's behalf.
        if session.get("admin_role") == "admin":
            return jsonify({"error": "administrators manage the platform and do not place calls"}), 403
        data = request.get_json(silent=True) or {}
        phone = str(data.get("phone", "")).strip()
        if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
            return jsonify({"error": "Phone must be a valid E.164 number"}), 400
        extension = str(data.get("extension") or "").strip()
        available = store.list_extensions() if session.get("admin_role") == "admin" else store.list_extensions(int(session["admin_user_id"]))
        if not any(row["extension"] == extension and row["active"] for row in available):
            return jsonify({"error": "Active extension is required"}), 400
        try:
            call = current_app.extensions["telephony_service"].start_outbound(
                phone=phone, extension=extension, contact_id=data.get("contact_id"), member_id=data.get("member_id"),
                caller_id_number=str(data.get("caller_id_number") or "").strip() or None,
            )
            return jsonify({"call": call.to_dict()}), 201
        except RuntimeError as exc:
            message = str(exc)
            if message.startswith(("No active callback number", "The selected callback number", "ARI event worker")):
                return jsonify({"error": message}), 400
            current_app.logger.exception("Admin call start failed")
            return jsonify({"error": "telephony service unavailable"}), 502
        except Exception:
            current_app.logger.exception("Admin call start failed")
            return jsonify({"error": "telephony service unavailable"}), 502

    @app.post("/admin/api/numbers/<path:number>/discontinue")
    @login_required
    def customer_discontinue_number(number):
        if session.get("admin_role") == "admin":
            return jsonify({"error": "Set the discontinuation date while editing the number"}), 400
        try:
            end_date = store.request_number_discontinuation(number, int(session["admin_user_id"]))
            apply_change()
            return jsonify({"ok": True, "discontinue_at": end_date})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/numbers/default")
    @login_required
    def admin_default_number():
        data = request.get_json(silent=True) or {}
        extension = str(data.get("extension") or "").strip()
        if session.get("admin_role") != "admin" and not any(row["extension"] == extension for row in store.list_extensions(int(session["admin_user_id"]))):
            return jsonify({"error": "extension not found"}), 404
        try:
            store.set_default_outbound_number(extension, str(data.get("number") or "").strip())
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/voicemails")
    @login_required
    def admin_voicemails():
        mailbox = request.args.get("extension", "").strip() or None
        owned = None
        if session.get("admin_role") != "admin":
            owned = [row["extension"] for row in store.list_extensions(int(session["admin_user_id"]))]
            if mailbox and mailbox not in owned:
                return jsonify({"error": "mailbox not found"}), 404
        folder = request.args.get("folder", "").strip() or None
        try:
            messages = current_app.extensions["voicemail_store"].list_messages(mailbox, folder) if mailbox or owned is None else [message for ext in owned for message in current_app.extensions["voicemail_store"].list_messages(ext, folder)]
            return jsonify({"voicemails": messages, "total": len(messages)})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/voicemails/<mailbox>/<folder>/<message>/file")
    @login_required
    def admin_voicemail_file(mailbox, folder, message):
        if not mailbox_allowed(mailbox):
            return jsonify({"error": "forbidden"}), 403
        path = current_app.extensions["voicemail_store"].audio_path(mailbox, folder, message)
        if not path:
            return jsonify({"error": "voicemail not found"}), 404
        return send_file(path, conditional=True, as_attachment=False, download_name=f"voicemail-{mailbox}-{message}{path.suffix}")

    @app.post("/admin/api/voicemails/<mailbox>/<folder>/<message>/read")
    @login_required
    def admin_voicemail_read(mailbox, folder, message):
        if not mailbox_allowed(mailbox):
            return jsonify({"error": "forbidden"}), 403
        if not current_app.extensions["voicemail_store"].mark_read(mailbox, folder, message):
            return jsonify({"error": "new voicemail not found"}), 404
        return jsonify({"ok": True})

    @app.delete("/admin/api/voicemails/<mailbox>/<folder>/<message>")
    @login_required
    def admin_voicemail_delete(mailbox, folder, message):
        if not mailbox_allowed(mailbox):
            return jsonify({"error": "forbidden"}), 403
        if not current_app.extensions["voicemail_store"].delete(mailbox, folder, message):
            return jsonify({"error": "voicemail not found"}), 404
        return jsonify({"ok": True})

    @app.post("/admin/api/webhooks")
    @login_required
    def admin_webhook():
        """Customers create their endpoints; an administrator manages the ones that
        exist - including editing them - because a broken endpoint is a call he
        cannot deliver."""
        try:
            data = request.get_json(silent=True) or {}
            if session.get("admin_role") == "admin":
                given = str(data.get("id") or "").strip()
                existing = next((row for row in store.list_webhooks() if str(row["id"]) == given), None) if given else None
                if not existing:
                    return jsonify({"error": "webhooks are created by the customer who owns them"}), 403
                # The endpoint's own owner decides the record; the request cannot
                # move a customer's endpoint to somebody else.
                result = store.save_webhook({**data, "id": existing["id"]}, existing["owner_user_id"])
                return jsonify({"ok": True, "webhook_id": result})
            owner = int(session["admin_user_id"])
            result = store.save_webhook(data, owner)
            return jsonify({"ok": True, "webhook_id": result})
        except INPUT_DB_ERRORS as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/webhooks/<int:webhook_id>")
    @login_required
    def admin_delete_webhook(webhook_id):
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            store.delete_webhook(webhook_id, owner)
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/admin/api/webhooks/<int:webhook_id>/test")
    @login_required
    def admin_test_webhook(webhook_id):
        owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
        if owner is not None and not any(row["id"] == webhook_id for row in store.list_webhooks(owner_user_id=owner)):
            return jsonify({"error": "webhook not found"}), 404
        service = current_app.extensions["telephony_service"]
        result = service.test_webhook(webhook_id)
        status = 200 if result.get("ok") else (404 if result.get("error") == "webhook not found" else 502)
        return jsonify(result), status

    @app.get("/admin/api/recordings/<call_id>/file")
    @login_required
    def admin_recording_file(call_id):
        service = current_app.extensions["telephony_service"]
        call = service.store.get(call_id)
        if call and session.get("admin_role") != "admin" and not any(row["extension"] == call.extension for row in store.list_extensions(int(session["admin_user_id"]))):
            return jsonify({"error": "recording not found"}), 404
        if not call or not call.recording_name or call.recording_status not in {"finalized", "available"}:
            return jsonify({"error": "recording not found"}), 404
        upstream = service.asterisk.open_stored_recording(call.recording_name, request.headers.get("Range"))
        if upstream is None:
            return jsonify({"error": "recording not found"}), 404
        headers = {"Content-Disposition": f'inline; filename="{call.recording_name}.{call.recording_format or "wav"}"'}
        for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if upstream.headers.get(header):
                headers[header] = upstream.headers[header]
        def body():
            try:
                yield from upstream.iter_content(64 * 1024)
            finally:
                upstream.close()
        return Response(
            stream_with_context(body()), status=upstream.status_code,
            content_type=upstream.headers.get("Content-Type", "application/octet-stream"), headers=headers,
            direct_passthrough=True,
        )

    @app.post("/admin/api/settings")
    @admin_required
    def admin_settings():
        try:
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict): raise ValueError("JSON object required")
            allowed = {
                "recording_enabled", "recording_format", "recording_retention_days", "recording_announcement",
                "recording_announcement_media", "recording_beep", "recording_max_duration_seconds",
                "default_extension", "inbound_fallback_extension", "webrtc_enabled",
                "service_host", "service_sip_port",
            }
            store.set_settings({k: data[k] for k in data if k in allowed})
            apply_change(); return jsonify({"ok": True})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/groups")
    @login_required
    def customer_group():
        """Create or rename a ring group. Customers manage their own; an
        administrator manages one for a customer from the workspace."""
        try:
            data = request.get_json(silent=True) or {}
            if session.get("admin_role") == "admin":
                # The administrator builds groups for the customer whose flow
                # they are editing; the workspace names that customer.
                chosen = str(data.get("owner_user_id") or "").strip()
                owner = int(chosen) if chosen.isdigit() else None
                if not owner:
                    return jsonify({"error": "Choose the customer this group belongs to"}), 400
            else:
                owner = int(session["admin_user_id"])
            data = {**data, "owner_user_id": owner}
            group_id = store.save_group(data, owner)
            store.add_activity(owner, int(session["admin_user_id"]), "group.saved", "extension_group", group_id, f"Group {data.get('name')} saved")
            apply_change(); return jsonify({"ok": True, "group_id": group_id})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/groups/<int:group_id>")
    @login_required
    def customer_group_delete(group_id):
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            group = store.get_group(group_id, owner)
            store.delete_group(group_id, owner)
            if group:
                store.add_activity(group["owner_user_id"], int(session["admin_user_id"]), "group.deleted", "extension_group", group_id, f"Group {group['name']} deleted")
            apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 404

    @app.get("/admin/api/customers/<int:customer_id>")
    @admin_required
    def admin_customer_detail(customer_id):
        customer = store.get_user(customer_id)
        if not customer or customer["role"] != "user":
            return jsonify({"error": "customer not found"}), 404
        extensions = store.list_extensions(customer_id)
        numbers = store.list_numbers(customer_id)
        calls, total = current_app.extensions["telephony_service"].store.search(extensions=[row["extension"] for row in extensions], limit=20)
        return jsonify({
            "customer": next((row for row in store.list_users() if row["id"] == customer_id), customer),
            "extensions": extensions, "numbers": numbers, "sip_accounts": store.list_sip_accounts(customer_id),
            "invoices": store.list_invoices(customer_id), "requests": store.list_requests(customer_id),
            "activity": store.list_activity(customer_id), "calls": [call.to_dict() for call in calls], "call_total": total,
            # The workspace routing tab manages this customer's flows, so it needs
            # the same three targets the customer sees: numbers, extensions, groups.
            "call_routes": store.list_call_routes(customer_id),
            "routing_flows": store.list_routing_flows(customer_id),
            "groups": store.list_groups(customer_id),
        })

    @app.post("/admin/api/requests")
    @login_required
    def customer_create_request():
        try:
            data = request.get_json(silent=True) or {}
            request_id = store.create_request(int(session["admin_user_id"]), str(data.get("request_type", "number")), str(data.get("details", "")))
            return jsonify({"ok": True, "request_id": request_id}), 201
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/requests/<int:request_id>/resolve")
    @admin_required
    def admin_resolve_request(request_id):
        try:
            data = request.get_json(silent=True) or {}
            store.resolve_request(request_id, str(data.get("status", "")), str(data.get("admin_note", "")), int(session["admin_user_id"]))
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/sip-accounts")
    @admin_required
    def admin_save_sip_account():
        try:
            data = request.get_json(silent=True) or {}
            owner = int(data.get("owner_user_id"))
            account_id = store.save_sip_account(data, owner)
            store.add_activity(owner, int(session["admin_user_id"]), "sip.saved", "sip_account", account_id, f"SIP account {data.get('label') or data.get('sip_username')} configured")
            store.add_notification(owner, "sip", "SIP credentials available", "A SIP account has been configured for your organization.")
            apply_change()
            return jsonify({"ok": True, "sip_account_id": account_id})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/sip-accounts/<int:account_id>")
    @admin_required
    def admin_delete_sip_account(account_id):
        try:
            store.delete_sip_account(account_id)
            apply_change()
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.get("/admin/api/device-status")
    @login_required
    def admin_device_status():
        # Use the same session-derived identity as the other endpoints: an
        # administrator sees every device, a customer only their own.
        is_admin = session.get("admin_role") == "admin"
        accounts = store.list_sip_accounts(None if is_admin else int(session["admin_user_id"]))
        try:
            endpoints = current_app.extensions["telephony_service"].asterisk.list_endpoints()
            live = {
                str(row.get("resource") or ""): str(row.get("state") or "offline").lower()
                for row in endpoints if str(row.get("technology") or "").lower() == "pjsip"
            }
        except Exception:
            live = {}
        def registration(row):
            username = str(row["sip_username"])
            safe_username = re.sub(r"[^A-Za-z0-9_-]", "-", username).strip("-")[:64]
            for resource in (str(row.get("extension") or ""), username, f"device-{safe_username}"):
                if resource and resource in live:
                    return "online" if live[resource] in {"online", "available"} else "offline"
            return row["registration_status"]

        return jsonify({"devices": [{"id": row["id"], "registration_status": registration(row)} for row in accounts]})

    @app.get("/admin/api/sip-accounts/<int:account_id>/credentials")
    @login_required
    def sip_account_credentials(account_id):
        owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
        rows = store.list_sip_accounts(owner, include_password=True)
        account = next((row for row in rows if row["id"] == account_id), None)
        if not account:
            return jsonify({"error": "SIP account not found"}), 404
        return jsonify({"sip_account": account})

    @app.post("/admin/api/call-routes")
    @login_required
    def save_customer_call_route():
        """One endpoint for every kind of call flow: a number, an extension or a
        group. The target decides which store call and who owns it."""
        try:
            data = request.get_json(silent=True) or {}
            target_type = str(data.get("target_type") or "number").strip().lower()
            target = str(data.get("target") or "").strip()
            owner = flow_owner(data, target_type, target, data.get("phone_number"))
            if target_type == "number":
                route_id = store.save_call_route(owner, data)
                label = data.get("phone_number")
            else:
                route_id = store.save_routing_flow(owner, data, target_type=target_type, target=target or None)
                label = f"{target_type} {target}"
            who = "operator" if session.get("admin_role") == "admin" else "customer"
            kind = "call_route" if target_type == "number" else "routing_flow"
            store.add_activity(owner, int(session["admin_user_id"]), "route.saved", kind, route_id, f"Call flow for {label} updated by the {who}")
            return jsonify({"ok": True, "route_id": route_id})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/call-routes/<int:route_id>")
    @login_required
    def delete_customer_call_route(route_id):
        """Removes an extension or group flow so it can be rebuilt from the
        default; number flows keep their own endpoint behaviour."""
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            store.delete_routing_flow(route_id, owner)
            apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 404

    @app.post("/admin/api/notifications/read-all")
    @login_required
    def read_all_customer_notifications():
        store.mark_all_notifications_read(int(session["admin_user_id"]))
        return jsonify({"ok": True})

    @app.post("/admin/api/notifications/<int:notification_id>/read")
    @login_required
    def read_customer_notification(notification_id):
        try:
            store.mark_notification_read(notification_id, int(session["admin_user_id"]))
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/admin/api/users")
    @admin_required
    def admin_save_user():
        try:
            data = request.get_json(silent=True) or {}
            data.update({"role": "user", "extension": ""})
            user_id = store.save_user(data, int(session["admin_user_id"]))
            return jsonify({"ok": True, "user_id": user_id})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/platformadmins")
    @admin_required
    def admin_save_platform_admin():
        try:
            data = request.get_json(silent=True) or {}
            data.update({"role": "admin", "extension": "", "active": True})
            user_id = store.save_user(data, int(session["admin_user_id"]))
            return jsonify({"ok": True, "user_id": user_id}), 201
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/users/<int:user_id>")
    @admin_required
    def admin_delete_user(user_id):
        try:
            store.delete_user(user_id, int(session["admin_user_id"]))
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/api-keys")
    @login_required
    def admin_create_api_key():
        if session.get("admin_role") == "admin":
            return jsonify({"error": "API keys are created by the customer who owns them"}), 403
        try:
            data = request.get_json(silent=True) or {}
            owner = int(session["admin_user_id"])
            key_id, token = store.create_api_key(str(data.get("name", "")), str(data.get("scopes", "*")), owner)
            store.add_activity(owner, int(session["admin_user_id"]), "api_key.created", "api_key", key_id, f"API key {data.get('name')} generated")
            return jsonify({"ok": True, "key_id": key_id, "token": token}), 201
        except (ValueError, sqlite3.IntegrityError, MySQLIntegrityError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/api-keys/<int:key_id>")
    @login_required
    def admin_update_api_key(key_id):
        try:
            data = request.get_json(silent=True) or {}
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            store.update_api_key(key_id, data, owner)
            key = next((row for row in store.list_api_keys(owner_user_id=owner) if row["id"] == key_id), None)
            if key:
                store.add_activity(key["owner_user_id"], int(session["admin_user_id"]), "api_key.updated",
                                   "api_key", key_id, f"API key {key['name']} updated")
            return jsonify({"ok": True})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/api-keys/<int:key_id>")
    @login_required
    def admin_revoke_api_key(key_id):
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            store.revoke_api_key(key_id, owner)
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/admin/api/invoices/<int:invoice_id>/status")
    @admin_required
    def admin_invoice_status(invoice_id):
        try:
            store.set_invoice_status(invoice_id, str((request.get_json(silent=True) or {}).get("status", "")))
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/invoices")
    @admin_required
    def admin_create_invoice():
        try:
            data = request.get_json(silent=True) or {}
            invoice_id = store.create_invoice(
                int(data.get("user_id")), str(data.get("number", "")),
                str(data.get("period_start", "")), str(data.get("period_end", "")),
                int(data.get("amount_cents", 500)), str(data.get("due_at", "")),
            )
            return jsonify({"ok": True, "invoice_id": invoice_id}), 201
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/email-config")
    @admin_required
    def admin_email_config():
        try:
            store.save_email_config(request.get_json(silent=True) or {})
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/email-config/test")
    @admin_required
    def admin_email_test():
        email = str((request.get_json(silent=True) or {}).get("email", "")).strip()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            return jsonify({"error": "Valid test email required"}), 400
        success, error = current_app.extensions["voicemail_notifier"].send_test(email)
        return jsonify({"ok": success, "error": error}), (200 if success else 502)

    @app.post("/admin/api/profile/recording")
    @login_required
    def admin_profile_recording():
        data = request.get_json(silent=True) or {}
        extension = str(data.get("extension") or session.get("admin_extension") or "")
        if session.get("admin_role") != "admin":
            owned = store.list_extensions(int(session["admin_user_id"]))
            if not extension and len(owned) == 1:
                extension = owned[0]["extension"]
            if not any(row["extension"] == extension for row in owned):
                return jsonify({"error": "Extension not found"}), 404
        if not extension:
            return jsonify({"error": "Choose an extension"}), 400
        try:
            enabled = bool(data.get("enabled", False))
            store.set_extension_recording(extension, enabled)
            return jsonify({"ok": True, "enabled": enabled})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/profile/email")
    @login_required
    def admin_profile_email():
        try:
            email = str((request.get_json(silent=True) or {}).get("email", ""))
            store.update_user_email(int(session["admin_user_id"]), email)
            session["admin_email"] = email.strip().lower()
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/password")
    @login_required
    def admin_password():
        password = str((request.get_json(silent=True) or {}).get("password", ""))
        if len(password) < 14 or len(password) > 256: return jsonify({"error": "Password must be between 14 and 256 characters"}), 400
        store.set_admin_password(int(session["admin_user_id"]), password)
        return jsonify({"ok": True})

    return store
