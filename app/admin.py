from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from flask import jsonify, redirect, request, session, send_from_directory
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
                    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'admin', active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS extensions (
                    extension TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', sip_username TEXT NOT NULL,
                    sip_password_enc TEXT NOT NULL, webrtc_enabled INTEGER NOT NULL DEFAULT 0,
                    recording_enabled INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS phone_numbers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT NOT NULL UNIQUE, provider TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '', inbound_extension TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sip_providers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, server TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 5060, username TEXT NOT NULL DEFAULT '', password_enc TEXT NOT NULL DEFAULT '',
                    transport TEXT NOT NULL DEFAULT 'udp', codecs TEXT NOT NULL DEFAULT 'ulaw,alaw',
                    allowed_ips TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(sip_providers)").fetchall()}
            if "allowed_ips" not in columns:
                db.execute("ALTER TABLE sip_providers ADD COLUMN allowed_ips TEXT NOT NULL DEFAULT ''")

    def ensure_bootstrap_admin(self, username, password):
        if not username or not password:
            return
        with self._connect() as db:
            if db.execute("SELECT id FROM admin_users LIMIT 1").fetchone() is None:
                db.execute("INSERT INTO admin_users(username,password_hash) VALUES(?,?)", (username, generate_password_hash(password, method="scrypt")))

    def bootstrap_telephony(self, config):
        with self._connect() as db:
            if db.execute("SELECT 1 FROM settings WHERE key='bootstrap_complete'").fetchone():
                return
            for ext in config.ASTERISK_EXTENSIONS:
                password = os.getenv(f"EXTENSION_{ext}_PASSWORD", "")
                if password:
                    db.execute("""INSERT OR IGNORE INTO extensions
                        (extension,display_name,sip_username,sip_password_enc,webrtc_enabled,recording_enabled,active)
                        VALUES(?,?,?,?,?,?,1)""", (ext, "", ext, self.encrypt(password), 0, 1))
            did = os.getenv("IPCOMMS_DID", "").strip()
            if did:
                db.execute("""INSERT OR IGNORE INTO phone_numbers
                    (number,provider,description,inbound_extension,active) VALUES(?,?,?,?,1)""", (did, "IPComms", "Primary DID", config.DEFAULT_EXTENSION))
            if os.getenv("IPCOMMS_SIP_SERVER") and os.getenv("IPCOMMS_SIP_USERNAME"):
                existing = db.execute("SELECT id FROM sip_providers WHERE name='IPComms'").fetchone()
                if existing is None:
                    db.execute("""INSERT INTO sip_providers(name,server,port,username,password_enc,transport,codecs,allowed_ips,active)
                        VALUES(?,?,?,?,?,?,?,?,1)""", (
                        "IPComms", os.getenv("IPCOMMS_SIP_SERVER", ""), int(os.getenv("IPCOMMS_SIP_PORT", "5060")),
                        os.getenv("IPCOMMS_SIP_USERNAME", ""), self.encrypt(os.getenv("IPCOMMS_SIP_PASSWORD", "")),
                        "udp", "ulaw,alaw", os.getenv("IPCOMMS_ALLOWED_IPS", "")))
            db.execute("INSERT INTO settings(key,value) VALUES('bootstrap_complete','true')")

    def authenticate(self, username, password):
        with self._connect() as db:
            row = db.execute("SELECT id,username,role,password_hash FROM admin_users WHERE username=? AND active=1", (username,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], password):
            return None
        return {"id": row["id"], "username": row["username"], "role": row["role"]}

    def set_admin_password(self, user_id, password):
        with self._connect() as db:
            db.execute("UPDATE admin_users SET password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (generate_password_hash(password, method="scrypt"), user_id))

    def list_extensions(self):
        with self._connect() as db:
            rows = db.execute("SELECT extension,display_name,sip_username,webrtc_enabled,recording_enabled,active FROM extensions ORDER BY extension").fetchall()
        return [dict(r) for r in rows]

    def get_extension_password(self, extension):
        with self._connect() as db:
            row = db.execute("SELECT sip_password_enc FROM extensions WHERE extension=?", (str(extension),)).fetchone()
        return self.decrypt(row["sip_password_enc"]) if row else ""

    def save_extension(self, data):
        extension = str(data.get("extension", "")).strip()
        if not extension.isdigit() or not 100 <= int(extension) <= 999:
            raise ValueError("Extension must be a 3-digit number from 100 to 999")
        username = str(data.get("sip_username") or extension).strip()
        password = str(data.get("sip_password") or "")
        if not username or len(username) > 80 or "\n" in username or "\r" in username:
            raise ValueError("Invalid SIP username")
        with self._connect() as db:
            existing = db.execute("SELECT sip_password_enc FROM extensions WHERE extension=?", (extension,)).fetchone()
            encrypted = self.encrypt(password) if password else (existing["sip_password_enc"] if existing else "")
            if not encrypted:
                raise ValueError("SIP password is required for a new extension")
            db.execute("""INSERT INTO extensions(extension,display_name,sip_username,sip_password_enc,webrtc_enabled,recording_enabled,active)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(extension) DO UPDATE SET display_name=excluded.display_name,sip_username=excluded.sip_username,
                sip_password_enc=excluded.sip_password_enc,webrtc_enabled=excluded.webrtc_enabled,recording_enabled=excluded.recording_enabled,
                active=excluded.active,updated_at=CURRENT_TIMESTAMP""", (
                extension, str(data.get("display_name", "")).strip()[:120], username, encrypted,
                int(bool(data.get("webrtc_enabled"))), int(bool(data.get("recording_enabled", True))), int(bool(data.get("active", True)))))
        return extension

    def delete_extension(self, extension):
        extension = str(extension).strip()
        with self._connect() as db:
            if db.execute("SELECT 1 FROM phone_numbers WHERE inbound_extension=?", (extension,)).fetchone():
                raise ValueError("Cannot delete an extension used by an inbound DID")
            result = db.execute("DELETE FROM extensions WHERE extension=?", (extension,))
            if result.rowcount == 0:
                raise ValueError("Extension not found")

    def list_numbers(self):
        with self._connect() as db:
            rows = db.execute("SELECT id,number,provider,description,inbound_extension,active FROM phone_numbers ORDER BY number").fetchall()
        return [dict(r) for r in rows]

    def save_number(self, data):
        number = str(data.get("number", "")).strip()
        if not number.startswith("+") or not number[1:].isdigit() or not 8 <= len(number) <= 16:
            raise ValueError("Phone number must be in E.164 format")
        inbound = str(data.get("inbound_extension", "")).strip()
        with self._connect() as db:
            if inbound and not db.execute("SELECT 1 FROM extensions WHERE extension=? AND active=1", (inbound,)).fetchone():
                raise ValueError("Inbound extension must be an active configured extension")
            if data.get("provider") and not db.execute("SELECT 1 FROM sip_providers WHERE name=? AND active=1", (str(data.get("provider")).strip(),)).fetchone():
                raise ValueError("Provider must be an active configured SIP provider")
            db.execute("""INSERT INTO phone_numbers(number,provider,description,inbound_extension,active) VALUES(?,?,?,?,?)
                ON CONFLICT(number) DO UPDATE SET provider=excluded.provider,description=excluded.description,
                inbound_extension=excluded.inbound_extension,active=excluded.active,updated_at=CURRENT_TIMESTAMP""", (
                number, str(data.get("provider", "")).strip()[:80], str(data.get("description", "")).strip()[:160], inbound[:3], int(bool(data.get("active", True)))))
        return number

    def delete_number(self, number):
        with self._connect() as db:
            result = db.execute("DELETE FROM phone_numbers WHERE number=?", (str(number).strip(),))
            if result.rowcount == 0:
                raise ValueError("Phone number not found")

    def list_providers(self):
        with self._connect() as db:
            rows = db.execute("SELECT id,name,server,port,username,transport,codecs,allowed_ips,active FROM sip_providers ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def list_provider_details(self):
        with self._connect() as db:
            rows = db.execute("SELECT id,name,server,port,username,password_enc,transport,codecs,allowed_ips,active FROM sip_providers ORDER BY name").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["password"] = self.decrypt(item.pop("password_enc"))
            result.append(item)
        return result

    def get_provider(self, name: str | None = None):
        with self._connect() as db:
            if name:
                row = db.execute("SELECT id,name,server,port,username,password_enc,transport,codecs,allowed_ips,active FROM sip_providers WHERE name=? AND active=1", (name,)).fetchone()
            else:
                row = db.execute("SELECT id,name,server,port,username,password_enc,transport,codecs,allowed_ips,active FROM sip_providers WHERE active=1 ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        item = dict(row)
        item["password"] = self.decrypt(item.pop("password_enc"))
        return item

    def first_active_number_for_provider(self, provider: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT number FROM phone_numbers WHERE provider=? AND active=1 ORDER BY id LIMIT 1", (provider,)).fetchone()
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
        name, server = str(data.get("name", "")).strip()[:80], str(data.get("server", "")).strip()[:255]
        if not name or not server or "\n" in name or "\r" in name or "\n" in server or "\r" in server:
            raise ValueError("Provider name and server are required")
        password = str(data.get("password") or "")
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
        with self._connect() as db:
            existing = db.execute("SELECT password_enc FROM sip_providers WHERE name=?", (name,)).fetchone()
            encrypted = self.encrypt(password) if password else (existing["password_enc"] if existing else "")
            if not encrypted:
                raise ValueError("SIP provider password is required for a new provider")
            db.execute("""INSERT INTO sip_providers(name,server,port,username,password_enc,transport,codecs,allowed_ips,active) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET server=excluded.server,port=excluded.port,username=excluded.username,password_enc=excluded.password_enc,
                transport=excluded.transport,codecs=excluded.codecs,allowed_ips=excluded.allowed_ips,active=excluded.active,updated_at=CURRENT_TIMESTAMP""", (
                name, server, port, str(data.get("username", "")).strip()[:120], encrypted, transport, codecs, allowed_ips, int(bool(data.get("active", True)))))
        return name

    def get_settings(self):
        with self._connect() as db:
            rows = db.execute("SELECT key,value FROM settings ORDER BY key").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_settings(self, values):
        with self._connect() as db:
            for key, value in values.items():
                if len(str(key)) > 100 or len(str(value)) > 2000:
                    raise ValueError("Setting is too long")
                db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP", (str(key), str(value)))

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
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                token = request.headers.get("X-CSRF-Token", "")
                expected = session.get("csrf_token", "")
                if not expected or not token or not secrets.compare_digest(token, expected):
                    return jsonify({"error": "csrf validation failed"}), 403
            return fn(*args, **kwargs)
        return wrapped

    def login_rate_limited():
        key = f"{request.remote_addr or 'unknown'}:{str((request.get_json(silent=True) or request.form).get('username', '')).strip().lower()[:80]}"
        now = time.monotonic()
        bucket = _LOGIN_BUCKETS[key]
        while bucket and now - bucket[0] >= _LOGIN_WINDOW:
            bucket.popleft()
        if len(bucket) >= _LOGIN_LIMIT:
            return True
        bucket.append(now)
        return False

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
        return jsonify({"username": session.get("admin_username"), "role": session.get("admin_role"), "csrf_token": session.get("csrf_token"),
                        "extensions": store.list_extensions(), "phone_numbers": store.list_numbers(), "providers": store.list_providers(), "settings": store.get_settings()})

    @app.post("/admin/api/extensions")
    @login_required
    def admin_extension():
        try:
            result = store.save_extension(request.get_json(silent=True) or {})
            apply_change(); return jsonify({"ok": True, "extension": result})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/extensions/<extension>")
    @login_required
    def admin_delete_extension(extension):
        try:
            store.delete_extension(extension); apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/numbers")
    @login_required
    def admin_number():
        try:
            result = store.save_number(request.get_json(silent=True) or {})
            apply_change(); return jsonify({"ok": True, "number": result})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.delete("/admin/api/numbers/<path:number>")
    @login_required
    def admin_delete_number(number):
        try:
            store.delete_number(number); apply_change(); return jsonify({"ok": True})
        except ValueError as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/providers")
    @login_required
    def admin_provider():
        try:
            result = store.save_provider(request.get_json(silent=True) or {})
            apply_change(); return jsonify({"ok": True, "provider": result})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/settings")
    @login_required
    def admin_settings():
        try:
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict): raise ValueError("JSON object required")
            allowed = {"recording_enabled", "recording_format", "recording_retention_days", "recording_announcement", "recording_announcement_media", "recording_beep",
                       "recording_max_duration_seconds", "default_extension", "inbound_fallback_extension", "webrtc_enabled"}
            store.set_settings({k: data[k] for k in data if k in allowed})
            apply_change(); return jsonify({"ok": True})
        except (ValueError, TypeError) as exc: return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/password")
    @login_required
    def admin_password():
        password = str((request.get_json(silent=True) or {}).get("password", ""))
        if len(password) < 14: return jsonify({"error": "Password must be at least 14 characters"}), 400
        store.set_admin_password(int(session["admin_user_id"]), password)
        return jsonify({"ok": True})

    return store
