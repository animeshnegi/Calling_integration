from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from flask import Response, current_app, jsonify, redirect, request, session, send_file, send_from_directory, stream_with_context
from werkzeug.security import check_password_hash, generate_password_hash


_LOGIN_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_WINDOW = 15 * 60
_LOGIN_LIMIT = 8


class SettingsStore:
    def __init__(self, path: str, secret_key: str):
        self.path = Path(path)
        self.secret_key = secret_key.encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
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
            extension_columns = {row["name"] for row in db.execute("PRAGMA table_info(extensions)").fetchall()}
            if "voicemail_enabled" not in extension_columns:
                db.execute("ALTER TABLE extensions ADD COLUMN voicemail_enabled INTEGER NOT NULL DEFAULT 0")
            if "voicemail_pin_enc" not in extension_columns:
                db.execute("ALTER TABLE extensions ADD COLUMN voicemail_pin_enc TEXT NOT NULL DEFAULT ''")
            if "voicemail_email" not in extension_columns:
                db.execute("ALTER TABLE extensions ADD COLUMN voicemail_email TEXT NOT NULL DEFAULT ''")

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
            if db.execute("SELECT 1 FROM settings WHERE key='bootstrap_complete'").fetchone():
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
                        (ext, "", ext, self.encrypt(password), 0, 1),
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
            db.execute("INSERT INTO settings(key,value) VALUES('bootstrap_complete','true')")

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

    def list_extensions(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT extension,display_name,sip_username,webrtc_enabled,recording_enabled,voicemail_enabled,voicemail_email,active FROM extensions ORDER BY extension"
            ).fetchall()
        return [dict(r) for r in rows]

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

    def save_extension(self, data):
        extension = str(data.get("extension", "")).strip()
        if not extension.isdigit() or not 100 <= int(extension) <= 999:
            raise ValueError("Extension must be a 3-digit number from 100 to 999")
        username = self._validate_config_value(data.get("sip_username") or extension, "SIP username", 80)
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
                "SELECT sip_password_enc,voicemail_enabled,voicemail_pin_enc FROM extensions WHERE extension=?", (extension,)
            ).fetchone()
            voicemail_enabled = bool(voicemail_enabled_value) if voicemail_enabled_value is not None else bool(existing and existing["voicemail_enabled"])
            encrypted = self.encrypt(password) if password else (existing["sip_password_enc"] if existing else "")
            if not encrypted:
                raise ValueError("SIP password is required for a new extension")
            voicemail_pin_enc = self.encrypt(voicemail_pin) if voicemail_pin else (existing["voicemail_pin_enc"] if existing else "")
            if voicemail_enabled and not voicemail_pin_enc:
                raise ValueError("Voicemail PIN is required when voicemail is enabled")
            db.execute(
                """INSERT INTO extensions(extension,display_name,sip_username,sip_password_enc,webrtc_enabled,recording_enabled,voicemail_enabled,voicemail_pin_enc,voicemail_email,active)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(extension) DO UPDATE SET display_name=excluded.display_name,sip_username=excluded.sip_username,
                sip_password_enc=excluded.sip_password_enc,webrtc_enabled=excluded.webrtc_enabled,recording_enabled=excluded.recording_enabled,
                voicemail_enabled=excluded.voicemail_enabled,voicemail_pin_enc=excluded.voicemail_pin_enc,voicemail_email=excluded.voicemail_email,
                active=excluded.active,updated_at=CURRENT_TIMESTAMP""",
                (
                    extension,
                    str(data.get("display_name", "")).strip()[:120],
                    username,
                    encrypted,
                    int(bool(data.get("webrtc_enabled"))),
                    int(bool(data.get("recording_enabled", True))),
                    int(voicemail_enabled),
                    voicemail_pin_enc,
                    voicemail_email,
                    int(bool(data.get("active", True))),
                ),
            )
        return extension

    def delete_extension(self, extension):
        extension = str(extension).strip()
        with self._connect() as db:
            if db.execute("SELECT 1 FROM phone_numbers WHERE inbound_extension=?", (extension,)).fetchone():
                raise ValueError("Cannot delete an extension used by an inbound DID")
            if db.execute(
                "SELECT 1 FROM settings WHERE key IN ('default_extension','inbound_fallback_extension') AND value=?",
                (extension,),
            ).fetchone():
                raise ValueError("Cannot delete a default or inbound fallback extension; change Call settings first")
            result = db.execute("DELETE FROM extensions WHERE extension=?", (extension,))
            if result.rowcount == 0:
                raise ValueError("Extension not found")

    def list_numbers(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,number,provider,description,inbound_extension,default_outbound,active FROM phone_numbers ORDER BY number"
            ).fetchall()
        return [dict(r) for r in rows]

    def save_number(self, data):
        number = str(data.get("number", "")).strip()
        if not number.startswith("+") or not number[1:].isdigit() or not 8 <= len(number) <= 16:
            raise ValueError("Phone number must be in E.164 format")
        inbound = str(data.get("inbound_extension", "")).strip()
        with self._connect() as db:
            if inbound and not db.execute(
                "SELECT 1 FROM extensions WHERE extension=? AND active=1", (inbound,)
            ).fetchone():
                raise ValueError("Inbound extension must be an active configured extension")
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
                """INSERT INTO phone_numbers(number,provider,description,inbound_extension,default_outbound,active) VALUES(?,?,?,?,?,?)
                ON CONFLICT(number) DO UPDATE SET provider=excluded.provider,description=excluded.description,
                inbound_extension=excluded.inbound_extension,default_outbound=excluded.default_outbound,
                active=excluded.active,updated_at=CURRENT_TIMESTAMP""",
                (
                    number,
                    str(data.get("provider", "")).strip()[:80],
                    str(data.get("description", "")).strip()[:160],
                    inbound[:3],
                    int(default_outbound),
                    int(bool(data.get("active", True))),
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
        with self._connect() as db:
            result = db.execute("DELETE FROM phone_numbers WHERE number=?", (str(number).strip(),))
            if result.rowcount == 0:
                raise ValueError("Phone number not found")

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

    def list_webhooks(self, include_tokens: bool = False):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,name,url,token_enc,events,active,created_at,updated_at FROM webhook_endpoints ORDER BY name"
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

    def save_webhook(self, data):
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
                existing = db.execute("SELECT id,token_enc FROM webhook_endpoints WHERE id=?", (int(webhook_id),)).fetchone()
            if existing is None:
                existing = db.execute("SELECT id,token_enc FROM webhook_endpoints WHERE name=?", (name,)).fetchone()
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
                "INSERT INTO webhook_endpoints(name,url,token_enc,events,active) VALUES(?,?,?,?,?)",
                (name, url, encrypted, events, int(bool(data.get("active", True)))),
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

    def list_webhook_deliveries(self, limit: int = 50):
        with self._connect() as db:
            rows = db.execute("SELECT id,endpoint_id,event,status,attempts,last_error,delivered_at,created_at,updated_at FROM webhook_deliveries ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def list_api_keys(self):
        with self._connect() as db:
            rows = db.execute("SELECT id,name,prefix,scopes,active,last_used_at,created_at FROM api_keys ORDER BY name").fetchall()
        return [dict(row) for row in rows]

    def create_api_key(self, name: str, scopes: str = "*"):
        name = self._validate_config_value(name, "API key name", 80)
        allowed = {"*", "calls:read", "calls:write", "recordings:read", "voicemail:read", "voicemail:write", "config:read", "webhooks:manage"}
        items = list(dict.fromkeys(item.strip() for item in str(scopes).split(",") if item.strip()))
        if not items or any(item not in allowed for item in items) or ("*" in items and len(items) != 1):
            raise ValueError("Invalid API key scopes")
        token = "eip_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            cursor = db.execute("INSERT INTO api_keys(name,prefix,key_hash,scopes) VALUES(?,?,?,?)", (name, token[:12], digest, ",".join(items)))
        return cursor.lastrowid, token

    def revoke_api_key(self, key_id: int):
        with self._connect() as db:
            result = db.execute("UPDATE api_keys SET active=0 WHERE id=?", (int(key_id),))
            if result.rowcount == 0:
                raise ValueError("API key not found")

    def authenticate_api_key(self, token: str, required_scope: str | None = None):
        if not token.startswith("eip_") or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            row = db.execute("SELECT id,name,scopes FROM api_keys WHERE key_hash=? AND active=1", (digest,)).fetchone()
            if not row:
                return None
            scopes = set(row["scopes"].split(","))
            if required_scope and "*" not in scopes and required_scope not in scopes:
                return False
            db.execute("UPDATE api_keys SET last_used_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        return {"id": row["id"], "name": row["name"], "scopes": list(scopes)}

    def claim_idempotency(self, client: str, request_key: str):
        with self._connect() as db:
            db.execute("DELETE FROM api_idempotency WHERE created_at<datetime('now','-24 hours')")
            row = db.execute("SELECT call_id FROM api_idempotency WHERE client=? AND request_key=?", (client, request_key)).fetchone()
            if row:
                return False, row["call_id"]
            try:
                db.execute("INSERT INTO api_idempotency(client,request_key) VALUES(?,?)", (client, request_key))
                return True, None
            except sqlite3.IntegrityError:
                row = db.execute("SELECT call_id FROM api_idempotency WHERE client=? AND request_key=?", (client, request_key)).fetchone()
                return False, row["call_id"] if row else None

    def finish_idempotency(self, client: str, request_key: str, call_id: str | None):
        with self._connect() as db:
            if call_id:
                db.execute("UPDATE api_idempotency SET call_id=? WHERE client=? AND request_key=?", (call_id, client, request_key))
            else:
                db.execute("DELETE FROM api_idempotency WHERE client=? AND request_key=?", (client, request_key))

    def delete_webhook(self, webhook_id):
        with self._connect() as db:
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
            rows = db.execute("SELECT id,username,email,extension,role,active,created_at,updated_at FROM admin_users ORDER BY username").fetchall()
        return [dict(row) for row in rows]

    def save_user(self, data, current_user_id: int | None = None):
        username = str(data.get("username", "")).strip().lower()
        email = str(data.get("email", "")).strip().lower()
        extension = str(data.get("extension", "")).strip()
        role = str(data.get("role", "user")).strip().lower()
        password = str(data.get("password") or "")
        if not re.fullmatch(r"[a-z0-9._-]{3,80}", username):
            raise ValueError("Username must be 3–80 letters, numbers, dots, underscores, or hyphens")
        if email and (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            raise ValueError("User email is invalid")
        if role not in {"admin", "user"}:
            raise ValueError("Role must be admin or user")
        if role == "user" and not extension:
            raise ValueError("An extension is required for a user account")
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
            if existing:
                db.execute("UPDATE admin_users SET username=?,email=?,extension=?,role=?,active=?,password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (username, email, extension, role, active, password_hash, existing["id"]))
                if role == "user" and extension and email:
                    db.execute("UPDATE extensions SET voicemail_email=?,updated_at=CURRENT_TIMESTAMP WHERE extension=?", (email, extension))
                return existing["id"]
            user_id = db.execute("INSERT INTO admin_users(username,email,extension,role,active,password_hash) VALUES(?,?,?,?,?,?)",
                                 (username, email, extension, role, active, password_hash)).lastrowid
            if role == "user" and extension and email:
                db.execute("UPDATE extensions SET voicemail_email=?,updated_at=CURRENT_TIMESTAMP WHERE extension=?", (email, extension))
            return user_id

    def delete_user(self, user_id: int, current_user_id: int):
        if int(user_id) == int(current_user_id):
            raise ValueError("You cannot delete your own account")
        with self._connect() as db:
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
        from_name = str(data.get("from_name", "EngineerIP Voicemail")).strip()[:120]
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

    def get_settings(self):
        with self._connect() as db:
            rows = db.execute("SELECT key,value FROM settings ORDER BY key").fetchall()
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
                db.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                    (str(key), str(value)),
                )

    def decrypt(self, ciphertext):
        from cryptography.fernet import Fernet
        key = base64.urlsafe_b64encode(hashlib.sha256(self.secret_key).digest())
        return Fernet(key).decrypt(ciphertext.encode()).decode()

    def encrypt(self, plaintext):
        from cryptography.fernet import Fernet
        key = base64.urlsafe_b64encode(hashlib.sha256(self.secret_key).digest())
        return Fernet(key).encrypt(plaintext.encode()).decode()


def register_admin(app, config, on_telephony_change=None):
    store = SettingsStore(config.SETTINGS_DB_PATH, config.SECRET_KEY)
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
                return redirect("/admin/login")
            user = store.get_user(int(session["admin_user_id"]))
            if not user or not user["active"]:
                session.clear()
                if request.path.startswith("/admin/api/"):
                    return jsonify({"error": "account disabled"}), 401
                return redirect("/admin/login")
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

    @app.get("/admin/login")
    def admin_login_page(): return send_from_directory(web_dir, "admin-login.html")

    @app.post("/admin/login")
    def admin_login():
        if session.get("admin_user_id"):
            return redirect("/admin")
        if login_rate_limited():
            return jsonify({"error": "too many login attempts"}), 429
        data = request.get_json(silent=True) or request.form
        user = store.authenticate(str(data.get("username", "")), str(data.get("password", "")))
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
        extensions = store.list_extensions()
        if not is_admin:
            extensions = [row for row in extensions if row["extension"] == assigned]
        voicemail_messages = current_app.extensions["voicemail_store"].list_messages(assigned) if assigned and not is_admin else current_app.extensions["voicemail_store"].list_messages()
        return jsonify({
            "username": session.get("admin_username"), "email": session.get("admin_email", ""),
            "assigned_extension": assigned, "role": session.get("admin_role"), "is_admin": is_admin,
            "csrf_token": session.get("csrf_token"), "extensions": extensions,
            "phone_numbers": store.list_numbers() if is_admin else [row for row in store.list_numbers() if row["inbound_extension"] == assigned],
            "providers": store.list_providers() if is_admin else [],
            "webhooks": store.list_webhooks() if is_admin else [],
            "webhook_deliveries": store.list_webhook_deliveries() if is_admin else [],
            "users": store.list_users() if is_admin else [],
            "api_keys": store.list_api_keys() if is_admin else [],
            "email_config": store.get_email_config() if is_admin else {},
            "email_deliveries": store.list_voicemail_deliveries() if is_admin else [],
            "call_summary": current_app.extensions["telephony_service"].store.summary(assigned if not is_admin else None),
            "voicemail_summary": {
                "total": len(voicemail_messages), "new": sum(row["folder"] == "inbox" for row in voicemail_messages),
                "old": sum(row["folder"] == "old" for row in voicemail_messages), "urgent": sum(row["folder"] == "urgent" for row in voicemail_messages),
            },
            "settings": store.get_settings() if is_admin else {},
        })

    @app.post("/admin/api/extensions")
    @admin_required
    def admin_extension():
        try:
            result = store.save_extension(request.get_json(silent=True) or {})
            apply_change(); return jsonify({"ok": True, "extension": result})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/extensions/<extension>")
    @admin_required
    def admin_delete_extension(extension):
        try:
            if current_app.extensions["voicemail_store"].list_messages(extension):
                raise ValueError("Cannot delete an extension that still has voicemail messages")
            store.delete_extension(extension); apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/numbers")
    @admin_required
    def admin_number():
        try:
            result = store.save_number(request.get_json(silent=True) or {})
            apply_change(); return jsonify({"ok": True, "number": result})
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
        if session.get("admin_role") != "admin":
            extension = str(session.get("admin_extension") or "")
            if not extension:
                return jsonify({"error": "no extension assigned"}), 403
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
            extension=extension or None, status=status or None, recordings_only=recordings_only,
            query=query or None, limit=limit, offset=offset,
        )
        return jsonify({"calls": [call.to_dict() for call in calls], "total": total, "limit": limit, "offset": offset})

    def mailbox_allowed(mailbox: str) -> bool:
        return session.get("admin_role") == "admin" or str(session.get("admin_extension") or "") == str(mailbox)

    @app.post("/admin/api/calls")
    @login_required
    def admin_start_call():
        data = request.get_json(silent=True) or {}
        phone = str(data.get("phone", "")).strip()
        if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
            return jsonify({"error": "Phone must be a valid E.164 number"}), 400
        extension = str(data.get("extension") or "").strip()
        if session.get("admin_role") != "admin":
            extension = str(session.get("admin_extension") or "")
        if not any(row["extension"] == extension and row["active"] for row in store.list_extensions()):
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

    @app.post("/admin/api/numbers/default")
    @login_required
    def admin_default_number():
        data = request.get_json(silent=True) or {}
        extension = str(data.get("extension") or "").strip()
        if session.get("admin_role") != "admin":
            extension = str(session.get("admin_extension") or "")
        try:
            store.set_default_outbound_number(extension, str(data.get("number") or "").strip())
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/admin/api/voicemails")
    @login_required
    def admin_voicemails():
        mailbox = request.args.get("extension", "").strip() or None
        if session.get("admin_role") != "admin":
            mailbox = str(session.get("admin_extension") or "")
            if not mailbox:
                return jsonify({"error": "no extension assigned"}), 403
        folder = request.args.get("folder", "").strip() or None
        try:
            messages = current_app.extensions["voicemail_store"].list_messages(mailbox, folder)
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
    @admin_required
    def admin_webhook():
        try:
            result = store.save_webhook(request.get_json(silent=True) or {})
            return jsonify({"ok": True, "webhook_id": result})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/webhooks/<int:webhook_id>")
    @admin_required
    def admin_delete_webhook(webhook_id):
        try:
            store.delete_webhook(webhook_id)
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/admin/api/webhooks/<int:webhook_id>/test")
    @admin_required
    def admin_test_webhook(webhook_id):
        service = current_app.extensions["telephony_service"]
        result = service.test_webhook(webhook_id)
        status = 200 if result.get("ok") else (404 if result.get("error") == "webhook not found" else 502)
        return jsonify(result), status

    @app.get("/admin/api/recordings/<call_id>/file")
    @login_required
    def admin_recording_file(call_id):
        service = current_app.extensions["telephony_service"]
        call = service.store.get(call_id)
        if call and session.get("admin_role") != "admin" and call.extension != str(session.get("admin_extension") or ""):
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
            }
            store.set_settings({k: data[k] for k in data if k in allowed})
            apply_change(); return jsonify({"ok": True})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/users")
    @admin_required
    def admin_save_user():
        try:
            user_id = store.save_user(request.get_json(silent=True) or {}, int(session["admin_user_id"]))
            return jsonify({"ok": True, "user_id": user_id})
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
    @admin_required
    def admin_create_api_key():
        try:
            data = request.get_json(silent=True) or {}
            key_id, token = store.create_api_key(str(data.get("name", "")), str(data.get("scopes", "*")))
            return jsonify({"ok": True, "key_id": key_id, "token": token}), 201
        except (ValueError, sqlite3.IntegrityError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/api-keys/<int:key_id>")
    @admin_required
    def admin_revoke_api_key(key_id):
        try:
            store.revoke_api_key(key_id)
            return jsonify({"ok": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

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
