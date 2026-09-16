from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from functools import wraps
from pathlib import Path

from flask import jsonify, redirect, request, session, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash


class SettingsStore:
    def __init__(self, path: str, secret_key: str):
        self.path = Path(path)
        self.secret_key = secret_key.encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'admin',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS extensions (
                    extension TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    sip_username TEXT NOT NULL,
                    sip_password_enc TEXT NOT NULL,
                    webrtc_enabled INTEGER NOT NULL DEFAULT 0,
                    recording_enabled INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS phone_numbers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    number TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    inbound_extension TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sip_providers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    server TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 5060,
                    username TEXT NOT NULL DEFAULT '',
                    password_enc TEXT NOT NULL DEFAULT '',
                    transport TEXT NOT NULL DEFAULT 'udp',
                    codecs TEXT NOT NULL DEFAULT 'ulaw,alaw',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def ensure_bootstrap_admin(self, username: str, password: str):
        if not username or not password:
            return
        with self._connect() as db:
            row = db.execute("SELECT id FROM admin_users LIMIT 1").fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO admin_users(username,password_hash) VALUES(?,?)",
                    (username, generate_password_hash(password, method="scrypt")),
                )

    def authenticate(self, username: str, password: str):
        with self._connect() as db:
            row = db.execute(
                "SELECT id,username,role FROM admin_users WHERE username=? AND active=1",
                (username,),
            ).fetchone()
            stored = db.execute(
                "SELECT password_hash FROM admin_users WHERE username=? AND active=1",
                (username,),
            ).fetchone()
        if not row or not stored or not check_password_hash(stored["password_hash"], password):
            return None
        return dict(row)

    def set_admin_password(self, user_id: int, password: str):
        with self._connect() as db:
            db.execute(
                "UPDATE admin_users SET password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (generate_password_hash(password, method="scrypt"), user_id),
            )

    def list_extensions(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT extension,display_name,sip_username,webrtc_enabled,recording_enabled,active FROM extensions ORDER BY extension"
            ).fetchall()
        return [dict(r) for r in rows]

    def save_extension(self, data):
        extension = str(data.get("extension", "")).strip()
        if not extension.isdigit() or not 100 <= int(extension) <= 999:
            raise ValueError("Extension must be a 3-digit number from 100 to 999")
        username = str(data.get("sip_username") or extension).strip()
        password = str(data.get("sip_password") or "")
        if not username or len(username) > 80:
            raise ValueError("Invalid SIP username")
        with self._connect() as db:
            existing = db.execute("SELECT sip_password_enc FROM extensions WHERE extension=?", (extension,)).fetchone()
            encrypted = self.encrypt(password) if password else (existing["sip_password_enc"] if existing else "")
            if not encrypted:
                raise ValueError("SIP password is required for a new extension")
            db.execute(
                """INSERT INTO extensions(extension,display_name,sip_username,sip_password_enc,webrtc_enabled,recording_enabled,active)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(extension) DO UPDATE SET display_name=excluded.display_name,sip_username=excluded.sip_username,
                   sip_password_enc=excluded.sip_password_enc,webrtc_enabled=excluded.webrtc_enabled,
                   recording_enabled=excluded.recording_enabled,active=excluded.active,updated_at=CURRENT_TIMESTAMP""",
                (extension, str(data.get("display_name", "")).strip()[:120], username, encrypted,
                 int(bool(data.get("webrtc_enabled"))), int(bool(data.get("recording_enabled", True))), int(bool(data.get("active", True)))),
            )
        return extension

    def list_numbers(self):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM phone_numbers ORDER BY number").fetchall()
        return [dict(r) for r in rows]

    def save_number(self, data):
        number = str(data.get("number", "")).strip()
        if not number.startswith("+") or not number[1:].isdigit() or not 8 <= len(number) <= 16:
            raise ValueError("Phone number must be in E.164 format")
        with self._connect() as db:
            db.execute(
                """INSERT INTO phone_numbers(number,provider,description,inbound_extension,active) VALUES(?,?,?,?,?)
                   ON CONFLICT(number) DO UPDATE SET provider=excluded.provider,description=excluded.description,
                   inbound_extension=excluded.inbound_extension,active=excluded.active,updated_at=CURRENT_TIMESTAMP""",
                (number, str(data.get("provider", "")).strip()[:80], str(data.get("description", "")).strip()[:160],
                 str(data.get("inbound_extension", "")).strip()[:3], int(bool(data.get("active", True)))),
            )
        return number

    def list_providers(self):
        with self._connect() as db:
            rows = db.execute("SELECT id,name,server,port,username,transport,codecs,active FROM sip_providers ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def save_provider(self, data):
        name = str(data.get("name", "")).strip()[:80]
        server = str(data.get("server", "")).strip()[:255]
        if not name or not server:
            raise ValueError("Provider name and server are required")
        password = str(data.get("password") or "")
        with self._connect() as db:
            existing = db.execute("SELECT password_enc FROM sip_providers WHERE name=?", (name,)).fetchone()
            encrypted = self.encrypt(password) if password else (existing["password_enc"] if existing else "")
            db.execute(
                """INSERT INTO sip_providers(name,server,port,username,password_enc,transport,codecs,active) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET server=excluded.server,port=excluded.port,username=excluded.username,
                   password_enc=excluded.password_enc,transport=excluded.transport,codecs=excluded.codecs,active=excluded.active,updated_at=CURRENT_TIMESTAMP""",
                (name, server, int(data.get("port", 5060)), str(data.get("username", "")).strip()[:120], encrypted,
                 str(data.get("transport", "udp")).lower(), str(data.get("codecs", "ulaw,alaw")).strip()[:120], int(bool(data.get("active", True)))),
            )
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
                db.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                    (str(key), str(value)),
                )

    def encrypt(self, plaintext: str) -> str:
        # Authenticated encryption using a key derived from the application SECRET_KEY.
        # The encrypted value is never returned by admin APIs.
        from cryptography.fernet import Fernet
        key = __import__("base64").urlsafe_b64encode(hashlib.sha256(self.secret_key).digest())
        return Fernet(key).encrypt(plaintext.encode()).decode()


def register_admin(app, config):
    store = SettingsStore(config.SETTINGS_DB_PATH, config.SECRET_KEY)
    store.ensure_bootstrap_admin(config.ADMIN_USERNAME, config.ADMIN_PASSWORD)
    app.extensions["settings_store"] = store

    def login_required(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("admin_user_id"):
                if request.path.startswith("/admin/api/"):
                    return jsonify({"error": "authentication required"}), 401
                return redirect("/admin/login")
            return fn(*args, **kwargs)
        return wrapped

    @app.get("/admin/login")
    def admin_login_page():
        return send_from_directory("../web", "admin-login.html")

    @app.post("/admin/login")
    def admin_login():
        if session.get("admin_user_id"):
            return redirect("/admin")
        data = request.get_json(silent=True) or request.form
        user = store.authenticate(str(data.get("username", "")), str(data.get("password", "")))
        if not user:
            return jsonify({"error": "invalid username or password"}), 401
        session.clear()
        session["admin_user_id"] = user["id"]
        session["admin_username"] = user["username"]
        session["admin_role"] = user["role"]
        return jsonify({"ok": True})

    @app.post("/admin/logout")
    @login_required
    def admin_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/admin")
    @login_required
    def admin_page():
        return send_from_directory("../web", "admin.html")

    @app.get("/admin/api/state")
    @login_required
    def admin_state():
        return jsonify({"username": session.get("admin_username"), "role": session.get("admin_role"),
                        "extensions": store.list_extensions(), "phone_numbers": store.list_numbers(),
                        "providers": store.list_providers(), "settings": store.get_settings()})

    @app.post("/admin/api/extensions")
    @login_required
    def admin_extension():
        try:
            extension = store.save_extension(request.get_json(silent=True) or {})
            return jsonify({"ok": True, "extension": extension})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/numbers")
    @login_required
    def admin_number():
        try:
            number = store.save_number(request.get_json(silent=True) or {})
            return jsonify({"ok": True, "number": number})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/providers")
    @login_required
    def admin_provider():
        try:
            name = store.save_provider(request.get_json(silent=True) or {})
            return jsonify({"ok": True, "provider": name})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/settings")
    @login_required
    def admin_settings():
        try:
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
            allowed = {"recording_enabled", "recording_format", "recording_retention_days", "recording_announcement",
                       "recording_beep", "default_extension", "inbound_fallback_extension", "webrtc_enabled"}
            store.set_settings({k: data[k] for k in data if k in allowed})
            return jsonify({"ok": True})
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/admin/api/password")
    @login_required
    def admin_password():
        data = request.get_json(silent=True) or {}
        password = str(data.get("password", ""))
        if len(password) < 14:
            return jsonify({"error": "Password must be at least 14 characters"}), 400
        store.set_admin_password(int(session["admin_user_id"]), password)
        return jsonify({"ok": True})

    return store
