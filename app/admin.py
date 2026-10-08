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
from .voicemail import mailbox_name

DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError, MySQLIntegrityError)
INPUT_DB_ERRORS = (ValueError, TypeError, sqlite3.IntegrityError, MySQLIntegrityError)


_LOGIN_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_WINDOW = 15 * 60
_LOGIN_LIMIT = 8


def extension_digits(value: Any) -> str:
    """The three digits a person dials: the part before `@` in an extension key."""
    return str(value or "").strip().split("@", 1)[0]


def extension_scope(value: Any) -> str:
    """The number an extension belongs to: the part after `@`, "" when it has none.

    Extensions are numbered per phone number - every number starts at 101 - so
    the three digits alone are not an identity any more. The stored key carries
    both: `101@+13025550001` is extension 101 on that line, and a bare `101` is a
    row with no number of its own - the platform's own devices, and rows written
    before numbers had their own extension sets.
    """
    text = str(value or "").strip()
    return text.split("@", 1)[1] if "@" in text else ""


def extension_key(digits: Any, scope: Any = "") -> str:
    """Build the identity string for an extension: digits, and the number it is on."""
    digits = extension_digits(digits)
    scope = str(scope or "").strip()
    return f"{digits}@{scope}" if scope else digits


def endpoint_name(value: Any) -> str:
    """The globally unique PJSIP identity of one extension.

    Digits plus the line they belong to - `101-13025550001` - so two 101s can
    never collide, `PJSIP/101` is never ambiguous, and the name carries no `@`
    (a PJSIP section would parse that as a key/value pair). A platform row keeps
    its digits: it answers only in the operator's own context.
    """
    digits = extension_digits(value)
    scope = extension_scope(value)
    if not digits:
        return str(value or "")
    if not scope:
        return digits
    return f"{digits}-{re.sub(r'[^0-9]', '', scope)}"


def extension_mailbox(key: Any) -> str:
    """The voicemail mailbox of an extension: unique, readable, filename-safe.

    A mailbox has to be unique inside the voicemail context, and extension
    numbers repeat across a customer's lines, so a scoped extension is
    `101-13025550001` while a row with no number of its own keeps the plain `101`
    its messages have always been filed under.
    """
    digits, scope = extension_digits(key), extension_scope(key)
    return f"{digits}-{re.sub(r'[^0-9]', '', scope)}" if scope else digits


def device_registration(account: dict, live: dict[str, str]) -> str:
    """Is this device account signed in?

    A phone registers under one of three names, depending on how it was set up:
    the extension it answers, the SIP username it authenticates as, or the
    generated device identity.
    """
    username = str(account.get("sip_username") or "")
    safe_username = re.sub(r"[^A-Za-z0-9_-]", "-", username).strip("-")[:64]
    key = str(account.get("extension") or "")
    endpoint = ""
    if extension_digits(key):
        # A device keyed to an extension is dialled - and so reported - under that
        # extension's endpoint name, the identity that carries the line.
        endpoint = endpoint_name(extension_key(
            extension_digits(key), extension_scope(key) or str(account.get("phone_number") or ""),
        ))
    for resource in (key, endpoint, username, f"device-{safe_username}"):
        if resource and resource in live:
            return "online" if live[resource] in {"online", "available"} else "offline"
    return str(account.get("registration_status") or "offline")


def device_account_for_extension(row: dict, accounts: list[dict]) -> dict | None:
    """The live device account that signs in as one extension, if there is one.

    A device is keyed to one extension of one line: it names that extension's key
    (`101@+13025550001`), or the digits while its own `phone_number` pins down
    the line. Nothing else counts, so the reception phone of one line never
    reports or rotates the secret of the other line's 101.
    """
    key = str(row.get("key") or extension_key(row.get("digits"), row.get("number") or ""))
    digits = str(row.get("digits") or extension_digits(key))
    number = str(row.get("number") or "")
    matches = []
    for account in accounts:
        if not account.get("active"):
            continue
        link = str(account.get("extension") or "").strip()
        if not link or extension_digits(link) != digits:
            continue
        if extension_scope(link):
            if link == key:
                matches.append(account)
            continue
        pinned = str(account.get("phone_number") or "")
        if not pinned or pinned == number:
            matches.append(account)
    return matches[0] if len(matches) == 1 else None


def extension_registration(extension: dict, live: dict[str, str], accounts: list[dict]) -> str:
    """Is any phone signed in as this extension, or as a device that answers it?

    Every name that answers for one extension is checked: the identity it logs in
    with, the unique endpoint name it is dialled on, and each device account
    keyed to this very line and extension - so two 101s are told apart by the
    number, never by the digits alone.
    """
    digits = str(extension.get("digits") or extension_digits(extension.get("extension")))
    number = str(extension.get("number") or extension_scope(extension.get("key") or extension.get("extension")))
    endpoint = str(extension.get("endpoint") or "")
    if not endpoint and digits:
        # A row built by hand (a test, an older caller) still names the endpoint
        # the renderer would give it: digits plus the line, or the digits alone
        # for a platform row.
        endpoint = endpoint_name(extension_key(digits, number))
    names = [str(extension.get("sip_username") or ""), endpoint]
    for account in accounts:
        if extension_digits(account.get("extension") or "") != digits:
            continue
        # A device answers this extension when it is keyed to this very line: the
        # account names either the key or the number, and both say the same.
        account_number = str(account.get("phone_number") or "") or extension_scope(account.get("extension") or "")
        if number and account_number and account_number != number:
            continue
        names.append(str(account.get("sip_username") or ""))
    # Every name that answers for this extension shares one set of contacts, so
    # any of them being online is this extension being signed in.
    states = [live[name] for name in names if name and name in live]
    if any(state in {"online", "available"} for state in states):
        return "online"
    if states:
        return "offline"
    # Asterisk answered and none of this extension's endpoints is signed in:
    # that is a phone that is not registered, which is the usual reason a call
    # does not ring. Asterisk not answering at all is a different case, and the
    # callers of this function leave the field out entirely for it.
    return "offline"


def same_account(left: Any, right: Any) -> bool:
    """Do these two owner_user_id values name the same account?

    A blank value is the platform's own resources, so "no owner" and "the
    platform" are the same thing - and extension numbers, devices and dial plans
    all have to agree on that.
    """
    def normalise(value: Any) -> str:
        return "" if value is None else str(value).strip()

    return normalise(left) == normalise(right)


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
        self._create_schema()
        # Extensions used to belong to the whole account. Migrating them onto the
        # number their inbound link names runs here, outside the schema
        # transaction, because it writes as it goes.
        self._migrate_extension_identity()

    def _create_schema(self):
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
                    id INTEGER PRIMARY KEY AUTOINCREMENT, extension TEXT NOT NULL DEFAULT '',
                    phone_number_id INTEGER,
                    display_name TEXT NOT NULL DEFAULT '', sip_username TEXT NOT NULL,
                    sip_password_enc TEXT NOT NULL, webrtc_enabled INTEGER NOT NULL DEFAULT 0,
                    recording_enabled INTEGER NOT NULL DEFAULT 1, voicemail_enabled INTEGER NOT NULL DEFAULT 0,
                    voicemail_pin_enc TEXT NOT NULL DEFAULT '', voicemail_email TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1, owner_user_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(phone_number_id, extension)
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
            # `101@+13025550001` is longer than three characters, and MySQL
            # declares these columns as String(3) - widen them before anything
            # else touches an extension, or a per-number extension is truncated
            # into somebody else's row.
            for table, column, definition in (
                ("extensions", "extension", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("customer_sip_accounts", "extension", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("phone_numbers", "inbound_extension", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("admin_users", "extension", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ):
                try:
                    db.execute(f"ALTER TABLE {table} MODIFY COLUMN {column} {definition}")
                except Exception:
                    pass
            extension_columns = {row["name"] for row in db.execute("PRAGMA table_info(extensions)").fetchall()}
            if "id" not in extension_columns or "phone_number_id" not in extension_columns:
                # SQLite cannot add a primary key in place. The table is rebuilt
                # with the identity it now has - a stable `id` and the number the
                # extension belongs to - and every existing row is copied across
                # exactly, so no device, mailbox or recording is lost.
                db.executescript("""
                    CREATE TABLE extensions_identity_v2 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, extension TEXT NOT NULL DEFAULT '',
                        phone_number_id INTEGER,
                        display_name TEXT NOT NULL DEFAULT '', sip_username TEXT NOT NULL,
                        sip_password_enc TEXT NOT NULL, webrtc_enabled INTEGER NOT NULL DEFAULT 0,
                        recording_enabled INTEGER NOT NULL DEFAULT 1, voicemail_enabled INTEGER NOT NULL DEFAULT 0,
                        voicemail_pin_enc TEXT NOT NULL DEFAULT '', voicemail_email TEXT NOT NULL DEFAULT '',
                        active INTEGER NOT NULL DEFAULT 1, owner_user_id INTEGER,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(phone_number_id, extension)
                    );
                    INSERT INTO extensions_identity_v2(
                        extension,display_name,sip_username,sip_password_enc,webrtc_enabled,recording_enabled,
                        voicemail_enabled,voicemail_pin_enc,voicemail_email,active,owner_user_id,created_at,updated_at)
                    SELECT extension,display_name,sip_username,sip_password_enc,webrtc_enabled,recording_enabled,
                        voicemail_enabled,voicemail_pin_enc,voicemail_email,active,owner_user_id,created_at,updated_at
                    FROM extensions;
                    DROP TABLE extensions;
                    ALTER TABLE extensions_identity_v2 RENAME TO extensions;
                """)
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
            # A platform extension (the operator's own phones, no customer line)
            # owns its digits platform-wide; a customer's extension is unique
            # inside its number, which the table constraint already enforces.
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_extensions_platform_digits ON extensions(extension) "
                "WHERE phone_number_id IS NULL AND owner_user_id IS NULL"
            )
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


    # An extension is identified by (phone_number_id, extension). The one-time
    # migration below moves every row from the shapes this platform used before
    # - the bare digits, then the `101@+13025550001` key - onto that identity.
    EXTENSION_IDENTITY_FLAG = "extension_identity_v2_initialized"

    def _ensure_extension_identity_schema(self) -> None:
        """Add `id` and `phone_number_id` to an existing extensions table.

        SQLite is rebuilt in `_create_schema` (it cannot add a primary key with
        ALTER TABLE); MySQL is patched in place here, because it can.
        """
        if not self.database.is_mysql:
            return
        columns = self.database.columns("extensions")
        with self._connect() as db:
            if "id" not in columns:
                try:
                    db.execute(
                        "ALTER TABLE extensions DROP PRIMARY KEY, "
                        "ADD COLUMN id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY FIRST"
                    )
                except Exception:
                    db.execute("ALTER TABLE extensions DROP PRIMARY KEY")
                    db.execute(
                        "ALTER TABLE extensions ADD COLUMN id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY FIRST"
                    )
            if "phone_number_id" not in columns:
                db.execute("ALTER TABLE extensions ADD COLUMN phone_number_id BIGINT NULL")
            try:
                db.execute(
                    "CREATE UNIQUE INDEX uq_extensions_number_extension ON extensions(phone_number_id,extension)"
                )
            except Exception:
                pass

    def _migrate_extension_identity(self) -> None:
        """One-time migration: extensions belong to a phone number.

        `101` on +13025550001 and `101` on +13025550002 are two different desks,
        so an extension is identified by the digits *and* the number. Rows used
        to carry their digits alone (`101`), and later a key with the number
        after an `@` (`101@+13025550001`); both are rewritten here to the digits
        plus `phone_number_id`, keeping each row's id, credentials, mailbox,
        recordings, owner and every flow that names it.

        A legacy row is placed on the number it answers for - the one whose
        inbound link names it, then the account's primary line - and a row that
        has no line at all is left unassigned (nothing can dial it, and the
        console reports it) rather than being merged into another extension.

        Runs once, after the schema connection is closed: SQLite serialises
        writers, so a migration inside an open transaction would wait on itself.
        """
        self._ensure_extension_identity_schema()
        with self._connect() as db:
            if db.execute("SELECT 1 FROM settings WHERE `key`=?", (self.EXTENSION_IDENTITY_FLAG,)).fetchone():
                return
            rows = db.execute(
                "SELECT id,extension,phone_number_id,owner_user_id FROM extensions ORDER BY id"
            ).fetchall()
            numbers = db.execute(
                "SELECT id,number,owner_user_id,inbound_extension,default_outbound,active FROM phone_numbers ORDER BY id"
            ).fetchall()
        by_number = {str(row["number"]): row for row in numbers}
        by_id = {int(row["id"]): row for row in numbers}

        def primary_number(owner_user_id):
            """The account's main line: its default outbound, else its first."""
            own = [row for row in numbers if same_account(row["owner_user_id"], owner_user_id)]
            if not own:
                return None
            for row in own:
                if row["default_outbound"]:
                    return row
            return own[0]

        # The new identity of every row, decided before anything is written.
        plan: list[dict] = []
        for row in rows:
            key = str(row["extension"] or "").strip()
            digits = extension_digits(key)
            scope = extension_scope(key)
            number_row = by_number.get(scope) if scope else None
            if number_row is None and row["phone_number_id"] not in (None, ""):
                number_row = by_id.get(int(row["phone_number_id"]))
            plan.append({
                "id": int(row["id"]), "digits": digits, "key": key,
                "number": number_row, "owner": row["owner_user_id"],
            })
        occupied: set[tuple[Any, str]] = set()
        for item in plan:
            if item["number"] is not None and item["digits"]:
                occupied.add((int(item["number"]["id"]), item["digits"]))
        unassigned: list[dict] = []
        for item in plan:
            if not item["digits"] or item["number"] is not None:
                continue
            if item["owner"] in (None, "") or not str(item["owner"]).strip().isdigit():
                # The platform's own extensions have no customer line: they stay
                # what they are and answer in the operator's own context.
                continue
            linked = [
                row for row in numbers
                if same_account(row["owner_user_id"], item["owner"])
                and extension_digits(row["inbound_extension"] or "") == item["digits"]
            ]
            targets = [*linked, primary_number(item["owner"])]
            for target in targets:
                if target is None:
                    continue
                slot = (int(target["id"]), item["digits"])
                if slot in occupied:
                    continue
                item["number"] = target
                occupied.add(slot)
                break
            else:
                unassigned.append(item)

        changed = 0
        with self._connect() as db:
            for item in plan:
                if not item["digits"]:
                    continue
                number_id = int(item["number"]["id"]) if item["number"] is not None else None
                row = db.execute(
                    "SELECT extension,phone_number_id FROM extensions WHERE id=?", (item["id"],)
                ).fetchone()
                if row is None:
                    continue
                if str(row["extension"]) != item["digits"] or row["phone_number_id"] != number_id:
                    db.execute(
                        "UPDATE extensions SET extension=?,phone_number_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (item["digits"], number_id, item["id"]),
                    )
                    changed += 1
            # Administrator profiles name an extension the short way: the digits
            # a person reads.
            for row in db.execute("SELECT id,extension FROM admin_users").fetchall():
                key = str(row["extension"] or "").strip()
                digits = extension_digits(key)
                if key and key != digits:
                    db.execute("UPDATE admin_users SET extension=? WHERE id=?", (digits, row["id"]))
                    changed += 1
            # A device account keeps the key that says which line's extension it
            # signs in as. A bare legacy link becomes that key while one row of
            # that account answers those digits - the line the device names, else
            # the only candidate - and is left alone when two lines could mean it:
            # nothing is merged, and nothing is guessed.
            for row in db.execute(
                "SELECT id,owner_user_id,phone_number,extension FROM customer_sip_accounts"
            ).fetchall():
                link = str(row["extension"] or "").strip()
                if not link or extension_scope(link):
                    continue
                owner = row["owner_user_id"]
                candidates = [
                    item for item in plan
                    if item["digits"] == extension_digits(link) and item["number"] is not None
                    and same_account(item["owner"], owner)
                ]
                pinned = str(row["phone_number"] or "")
                if pinned:
                    named = [item for item in candidates if str(item["number"]["number"]) == pinned]
                    candidates = named or candidates
                if len(candidates) != 1:
                    continue
                db.execute(
                    "UPDATE customer_sip_accounts SET extension=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (extension_key(candidates[0]["digits"], str(candidates[0]["number"]["number"])), row["id"]),
                )
                changed += 1
            # A number's inbound link stays a key - that is how it names one
            # extension among several with the same digits across accounts - and a
            # bare legacy link becomes the key of the row that answers this line.
            for row in db.execute("SELECT id,number,inbound_extension FROM phone_numbers").fetchall():
                link = str(row["inbound_extension"] or "").strip()
                if not link or extension_scope(link):
                    continue
                attached = next(
                    (item for item in plan if item["digits"] == extension_digits(link)
                     and item["number"] is not None and str(item["number"]["number"]) == str(row["number"])),
                    None,
                )
                if attached is not None:
                    db.execute(
                        "UPDATE phone_numbers SET inbound_extension=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (extension_key(attached["digits"], row["number"]), row["id"]),
                    )
                    changed += 1
            for key in ("default_extension", "inbound_fallback_extension"):
                value = db.execute("SELECT value FROM settings WHERE `key`=?", (key,)).fetchone()
                digits = extension_digits(value["value"]) if value else ""
                if digits and str(value["value"]) != digits:
                    db.execute("UPDATE settings SET value=? WHERE `key`=?", (digits, key))
                    changed += 1
            if unassigned:
                # Visible, not silent: nothing dials an extension with no number.
                db.execute(
                    "INSERT INTO settings(`key`,value) VALUES('extensions_unassigned',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                    (json.dumps([
                        {"id": item["id"], "extension": item["digits"]} for item in unassigned
                    ]),),
                )
            db.execute(
                "INSERT INTO settings(`key`,value) VALUES(?,'true') "
                "ON CONFLICT(key) DO UPDATE SET value='true',updated_at=CURRENT_TIMESTAMP",
                (self.EXTENSION_IDENTITY_FLAG,),
            )
        self.remap_extension_references()
        return changed

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

    def _account_label(self, owner_user_id: int | None) -> str:
        """How an account is named in a message: its company, or the platform."""
        if owner_user_id is None:
            return "the platform"
        with self._connect() as db:
            row = db.execute("SELECT username,company_name FROM admin_users WHERE id=?", (int(owner_user_id),)).fetchone()
        if not row:
            return "another account"
        return str(row["company_name"] or row["username"] or "another account")

    def _extension_view(self, item: dict) -> dict:
        """A row the rest of the app reads: the digits and the number they are on.

        `extension` is the three digits - what the database stores - and `key` is
        the identity every reference, flow, mailbox and endpoint uses, e.g.
        `101@+13025550001`. An extension with no phone number has no key beyond
        its digits, and nothing dials it: that is the point, not an oversight.
        """
        digits = extension_digits(item.get("extension"))
        number = str(item.get("number") or "").strip()
        item["extension"] = digits
        item["digits"] = digits
        item["number"] = number
        item["key"] = extension_key(digits, number)
        item["mailbox"] = extension_mailbox(item["key"])
        item["endpoint"] = endpoint_name(item["key"])
        return item

    def list_extensions(self, owner_user_id: int | None = None):
        """Every extension row, ordered by the number it belongs to and then its digits.

        The identity is (phone_number_id, extension): two numbers of one customer
        each start their own set at 101, and the digits alone never name a device
        outside the number the call is on.
        """
        with self._connect() as db:
            where = " WHERE e.owner_user_id=?" if owner_user_id is not None else ""
            rows = db.execute(
                "SELECT e.id,e.extension,e.phone_number_id,e.display_name,e.sip_username,e.webrtc_enabled,"
                "e.recording_enabled,e.voicemail_enabled,e.voicemail_email,e.active,e.owner_user_id,n.number AS number "
                "FROM extensions e LEFT JOIN phone_numbers n ON n.id=e.phone_number_id" + where,
                (int(owner_user_id),) if owner_user_id is not None else (),
            ).fetchall()
        return sorted(
            (self._extension_view(dict(row)) for row in rows),
            key=lambda row: (row["number"], int(row["digits"]) if row["digits"].isdigit() else 0),
        )

    def endpoint_name(self, extension: str) -> str:
        """The globally unique PJSIP identity of one extension.

        Digits plus the line they belong to (`101-13025550001`): two 101s can
        never collide, `PJSIP/101` is never ambiguous, and no name carries the
        `@` a PJSIP section cannot hold. A platform row keeps its digits - it
        answers only in the operator's own context.
        """
        return endpoint_name(extension)

    def extensions_on_number(self, owner_user_id: int | None, number: str, active_only: bool = True) -> list[dict]:
        """The extensions that belong to one phone number: its own set, from 101 up."""
        number = str(number or "").strip()
        return [
            row for row in self.list_extensions(owner_user_id)
            if row["number"] == number and (not active_only or row["active"])
        ]

    def unassigned_extensions(self, owner_user_id: int | None = None, active_only: bool = True) -> list[dict]:
        """Extensions with no phone number.

        They are the platform's own devices (no customer, the operator's context)
        or rows an operator has not put on a line yet. Either way no three-digit
        dial reaches them: an extension answers only on the number it is on.
        """
        return [
            row for row in self.list_extensions(owner_user_id)
            if not row["number"] and (not active_only or row["active"])
        ]

    def _link_is(self, link: Any, key: str, owner_user_id: int | None) -> bool:
        """Does a number's stored inbound link name this extension?"""
        text = str(link or "").strip()
        if not text:
            return False
        if text == str(key):
            return True
        if extension_scope(text):
            return False
        return extension_digits(text) == extension_digits(key)

    def line_devices(self, owner_user_id: int | None, number: str, active_only: bool = True) -> list[str]:
        """The extension keys a line's generated flow rings: its own set, in dialling order."""
        rows = self.extensions_on_number(owner_user_id, number, active_only)
        keys = []
        for row in sorted(rows, key=lambda item: int(item["digits"]) if item["digits"].isdigit() else 0):
            if row["key"] not in keys:
                keys.append(str(row["key"]))
        return keys

    def resolve_extension(self, number: str, digits: Any, owner_user_id: int | None = None):
        """Which extension answers these digits on this number - and only here.

        Three digits always resolve inside the phone number the call is on: `104`
        on +13025550001 is that line's 104. A line that has no 104 has no 104 -
        there is deliberately no fallback to another number, to the account's
        lowest line, to the platform's own rows or to another customer.
        """
        wanted = extension_digits(digits)
        number = str(number or "").strip()
        if not wanted or not number:
            return None
        rows = self.list_extensions(owner_user_id)
        return next((row for row in rows if row["number"] == number and row["digits"] == wanted), None)

    def extension_key_for(self, number: str, digits: Any, owner_user_id: int | None = None) -> str:
        """The key of the extension that answers these digits on this number."""
        row = self.resolve_extension(number, digits, owner_user_id)
        return str(row["key"]) if row else ""

    def call_extension_names(self, owner_user_id: int | None) -> list[str]:
        """Every extension name this account's call records may carry.

        A record names the exact extension - its key, `101@+13025550001` - so two
        101s are never the same row in a history. A record written before that,
        or by a caller that only gave the digits, is this account's while exactly
        one extension on the whole platform answers those digits; that keeps an
        older install's history readable without ever mixing two customers' 101s.
        """
        rows = self.list_extensions(owner_user_id)
        names = [str(row["key"]) for row in rows]
        if owner_user_id is not None and rows:
            seen: dict[str, int] = {}
            for row in self.list_extensions():
                seen[row["digits"]] = seen.get(row["digits"], 0) + 1
            names.extend(row["digits"] for row in rows if seen.get(row["digits"]) == 1)
        return names

    def resolve_requested_extension(self, value: Any, owner_user_id: int | None = None) -> tuple[str, str]:
        """An API request's extension as its key, or the reason it cannot be one.

        A key names one extension exactly. Bare digits name the single row of
        this account that carries them; when several rows carry them the request
        is ambiguous - the caller has to name the phone number - and nothing is
        guessed.
        """
        text = str(value or "").strip()
        if not text:
            return "", "Extension is required"
        if extension_scope(text):
            row = self._extension_row(text)
            if row is None or (
                owner_user_id is not None and not same_account(row.get("owner_user_id"), owner_user_id)
            ):
                return "", "Extension not found"
            return str(row["key"]), ""
        digits = extension_digits(text)
        if not digits.isdigit():
            return "", "Extension not found"
        rows = [row for row in self.list_extensions(owner_user_id) if row["digits"] == digits]
        if not rows:
            return "", "Extension not found"
        # A row with no number of its own is named by its digits - that is its
        # key - so the plain digits are exact here, never a guess.
        if any(str(row["key"]) == digits for row in rows):
            return digits, ""
        if len(rows) > 1:
            return "", (
                f"{digits} is on more than one of this account's numbers - "
                "name the phone number as well"
            )
        return str(rows[0]["key"]), ""

    def extension_key_for_digits(self, digits: Any, owner_user_id: int | None = None) -> str:
        """The key of the one row these digits name, or "" when several do.

        This is the explicit-ambiguity rule: a caller that knows only `101` gets
        an answer while exactly one of the account's extensions carries it, and
        must name the phone number when more than one does - guessing would ring
        the wrong desk.
        """
        wanted = extension_digits(digits)
        if not wanted:
            return ""
        rows = [row for row in self.list_extensions(owner_user_id) if row["digits"] == wanted]
        return str(rows[0]["key"]) if len(rows) == 1 else ""

    def scoped_extension_keys(self, number: str, owner_user_id: int | None, *, include_platform: bool = False) -> list[str]:
        """Every extension an inbound call on this number may reach, by key.

        Exactly one number's own set. Another of the account's numbers is a
        different line with its own set, so its extensions do not answer here;
        the platform's own rows belong to the operator's line and are not part of
        any customer's number either.
        """
        keys = [row["key"] for row in self.extensions_on_number(owner_user_id, number)]
        if include_platform and owner_user_id is not None:
            keys.extend(
                row["key"] for row in self.unassigned_extensions(None)
                if row.get("owner_user_id") in (None, "")
            )
        seen, ordered = set(), []
        for key in keys:
            if key not in seen:
                seen.add(key)
                ordered.append(key)
        return ordered

    def get_extension_owner(self, extension: str) -> int | None:
        """The account that holds this extension key.

        The key carries its number, so the answer is exact: one line's 101 can
        belong to a different account than another's without either being
        guessed at. Digits on their own name a row only while exactly one
        extension platform-wide carries them.
        """
        key = str(extension or "").strip()
        digits, scope = extension_digits(key), extension_scope(key)
        if not digits:
            return None
        with self._connect() as db:
            if scope:
                row = db.execute(
                    "SELECT e.owner_user_id FROM extensions e JOIN phone_numbers n ON n.id=e.phone_number_id "
                    "WHERE e.extension=? AND n.number=?",
                    (digits, scope),
                ).fetchone()
                return row["owner_user_id"] if row else None
            plain = db.execute(
                "SELECT owner_user_id FROM extensions WHERE extension=? AND phone_number_id IS NULL",
                (digits,),
            ).fetchone()
            if plain is not None:
                # A row with no line of its own is named by its digits alone:
                # the digits are its key, so the answer is exact.
                return plain["owner_user_id"]
            rows = db.execute("SELECT owner_user_id FROM extensions WHERE extension=?", (digits,)).fetchall()
        owners = {item["owner_user_id"] for item in rows}
        return owners.pop() if len(owners) == 1 else None

    def extension_row(self, extension):
        """One extension row, named by its key or - while one row carries them - by its digits."""
        return self._extension_row(extension)

    def _extension_row(self, extension):
        """One extension row by key (`101@+13025550001`) or, when unique, by digits."""
        key = str(extension or "").strip()
        digits, scope = extension_digits(key), extension_scope(key)
        if not digits:
            return None
        columns = (
            "id,extension,display_name,sip_username,sip_password_enc,owner_user_id,voicemail_enabled,"
            "voicemail_pin_enc,recording_enabled,webrtc_enabled,active,phone_number_id"
        )
        with self._connect() as db:
            if scope:
                rows = db.execute(
                    f"SELECT {columns} FROM extensions WHERE extension=? AND phone_number_id="
                    "(SELECT id FROM phone_numbers WHERE number=?)",
                    (digits, scope),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT {columns} FROM extensions WHERE extension=? AND phone_number_id IS NULL", (digits,)
                ).fetchall()
                if not rows:
                    # Nothing carries the plain digits as its own identity: they
                    # name the single row that answers them, and nothing at all
                    # while several do.
                    rows = db.execute(f"SELECT {columns} FROM extensions WHERE extension=?", (digits,)).fetchall()
            if len(rows) != 1:
                return None
            item = dict(rows[0])
            number = db.execute(
                "SELECT number FROM phone_numbers WHERE id=?", (item["phone_number_id"],)
            ).fetchone() if item["phone_number_id"] else None
            item["number"] = str(number["number"]) if number else ""
        return self._extension_view(item)

    def get_extension_password(self, extension):
        row = self._extension_row(extension)
        return self.decrypt(row["sip_password_enc"]) if row else ""

    def get_voicemail_pin(self, extension):
        row = self._extension_row(extension)
        if row is None:
            row = self._voicemail_aliases().get(str(extension))
        return self.decrypt(row["voicemail_pin_enc"]) if row and row["voicemail_pin_enc"] else ""

    # A mailbox that has to be longer than the digits (two lines both holding a
    # 101) still needs a pin, and the customer reads it off the same sheet. The
    # alias file records where it came from.
    VOICEMAIL_PIN_ALIASES_KEY = "voicemail_pin_aliases"

    def _voicemail_aliases(self) -> dict:
        try:
            stored = json.loads(self.get_settings().get(self.VOICEMAIL_PIN_ALIASES_KEY) or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(stored, dict):
            return {}
        aliases = {}
        for mailbox, source in stored.items():
            row = self._extension_row(str(source))
            if row:
                aliases[str(mailbox)] = row
        return aliases

    def alias_voicemail_pin(self, mailbox: str, source: str) -> None:
        """Remember that this mailbox's pin is the one of `source`."""
        mailbox, source = str(mailbox), str(source)
        if mailbox == source:
            return
        try:
            stored = json.loads(self.get_settings().get(self.VOICEMAIL_PIN_ALIASES_KEY) or "{}")
        except (TypeError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        if stored.get(mailbox) == source:
            return
        stored[mailbox] = source
        with self._connect() as db:
            db.execute(
                "INSERT INTO settings(`key`,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                (self.VOICEMAIL_PIN_ALIASES_KEY, json.dumps(stored, sort_keys=True)),
            )

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

    def add_extension_to_number(self, number: str, owner_user_id: int | None = None, data: dict | None = None) -> dict:
        """Add the next extension to a phone number.

        Every number's extensions start at 101 and go on from there, so this is
        what the console's "Add extension" does on a line: the next free number
        on *this* number, provisioned with credentials and a default flow.
        """
        number = str(number or "").strip()
        if not number:
            raise ValueError("Choose the phone number this extension belongs to")
        owner = int(owner_user_id) if owner_user_id is not None else self.get_number_owner(number)
        if owner is None:
            raise ValueError("Only a number assigned to a customer can hold extensions")
        if not self._number_belongs_to(owner, number):
            raise ValueError("Number is not assigned to this customer")
        payload = dict(data or {})
        # The console may name the digits it wants ("104"); a request without
        # them gets the next free one. Either way the pair (number, digits) is
        # what an extension is, so a clash is on this very line.
        wanted = extension_digits(payload.get("extension") or payload.get("digits") or "")
        if wanted and not (wanted.isdigit() and 100 <= int(wanted) <= 999):
            raise ValueError("Extension must be a 3-digit number from 100 to 999")
        if wanted and any(row["digits"] == wanted for row in self.extensions_on_number(owner, number)):
            raise ValueError(f"Extension {wanted} already answers on {number}")
        digits = wanted or self.next_extension_number(owner, number)
        payload.update({"extension": digits, "number": number})
        payload.setdefault("active", True)
        self.save_extension(payload, owner)
        return next(
            row for row in self.list_extensions(owner)
            if row["digits"] == digits and row["number"] == str(number)
        )

    def get_number_owner(self, number: str) -> int | None:
        with self._connect() as db:
            row = db.execute("SELECT owner_user_id FROM phone_numbers WHERE number=?", (str(number),)).fetchone()
        return row["owner_user_id"] if row else None

    def remap_extension_references(self) -> int:
        """Rewrite stored flow and group references from digits to their keys.

        After the migration a reference like "101" can point at several rows, so
        every flow is rewritten to the exact key that the line it belongs to
        answers to. References that resolve to nothing are left alone: a
        validator will report them if that flow is ever saved.
        """
        changed = 0
        for flow in self.list_call_routes():
            owner = flow.get("owner_user_id")
            number = flow.get("phone_number")
            route = flow.get("route") or {}
            rewritten = self._remap_nodes(route.get("nodes") or [], int(owner) if owner is not None else None, number)
            if rewritten:
                self.save_call_route(int(owner), {
                    "phone_number": number, "name": flow.get("name") or "Call flow",
                    "route": route, "active": bool(flow.get("active", True)),
                })
                changed += 1
        for flow in self.list_routing_flows():
            owner = flow.get("owner_user_id")
            route = flow.get("route") or {}
            rewritten = self._remap_nodes(route.get("nodes") or [], int(owner) if owner is not None else None, "")
            if rewritten:
                self.save_routing_flow(
                    int(owner),
                    {"name": flow.get("name") or "Call flow", "route": route, "active": bool(flow.get("active", True))},
                    target_type=flow["target_type"], target=flow["target"],
                )
                changed += 1
        for group in self.list_groups():
            members = list(group.get("members") or [])
            rewritten = [
                self._resolve_reference(member, group.get("owner_user_id"), "") or member for member in members
            ]
            if rewritten != members:
                with self._connect() as db:
                    db.execute(
                        "UPDATE extension_groups SET members=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (",".join(str(item) for item in rewritten), group["id"]),
                    )
                changed += 1
        return changed

    def _resolve_reference(self, value: Any, owner: Any, number: str) -> str:
        """One stored extension reference, resolved to a key ("" if unknown)."""
        value = str(value or "").strip()
        if not value:
            return ""
        owner_id = int(owner) if str(owner or "").strip().isdigit() else None
        rows = self.list_extensions(owner_id)
        digits, scope = extension_digits(value), extension_scope(value)
        if scope:
            # A key names one line's extension exactly; anything else is unknown.
            return next((row["key"] for row in rows if row["digits"] == digits and row["number"] == scope), "")
        if any(row["key"] == value for row in rows):
            return value
        if number:
            resolved = self.extension_key_for(number, digits, owner_id)
            if resolved:
                return resolved
        return self.extension_key_for_digits(digits, owner_id)

    def _remap_nodes(self, nodes: list, owner: int | None, number: str) -> bool:
        """Rewrite the extension references inside one flow's nodes, in place."""
        changed = False
        scope = str(number or "").strip()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "extension":
                resolved = self._resolve_reference(node.get("extension"), owner, scope)
                if resolved and resolved != str(node.get("extension")):
                    node["extension"] = resolved
                    changed = True
            elif node.get("type") == "voicemail":
                resolved = self._resolve_reference(node.get("mailbox"), owner, scope)
                if resolved and resolved != str(node.get("mailbox")):
                    node["mailbox"] = resolved
                    changed = True
            elif node.get("type") == "ivr":
                fallback = self._resolve_reference(node.get("fallback"), owner, scope)
                if fallback and fallback != str(node.get("fallback")):
                    node["fallback"] = fallback
                    changed = True
            for value in (node.get("extensions") or []):
                resolved = self._resolve_reference(value, owner, scope)
                if resolved and resolved != str(value):
                    node["extensions"] = [resolved if str(item) == str(value) else item for item in node["extensions"]]
                    changed = True
        return changed

    def _number_belongs_to(self, owner_user_id: int | None, number: str) -> bool:
        """Is this DID one of the account's own numbers?"""
        number = str(number or "").strip()
        if not number:
            return False
        return any(
            str(row["number"]) == number and same_account(row.get("owner_user_id"), owner_user_id)
            for row in self.list_numbers()
        )

    def extension_number_row(self, number: Any):
        """The phone_numbers row this number names, or None (see `same_account`)."""
        text = str(number or "").strip()
        if not text:
            return None
        with self._connect() as db:
            return db.execute(
                "SELECT id,number,owner_user_id,provider,inbound_extension,default_outbound,active "
                "FROM phone_numbers WHERE number=?", (text,)
            ).fetchone()

    def save_extension(self, data, owner_user_id: int | None = None, enforce_owner: bool = False):
        """Create or edit one extension, on one of the account's numbers.

        An extension is identified by the phone number it is on and its digits:
        every line starts its own set at 101, so `104` means the 104 of the line
        the call is on. Creating one therefore validates the (phone number,
        digits) pair. A request that names only the digits is accepted while
        exactly one of the account's extensions carries them - naming several is
        an error that asks for the phone number, never a guess - and a customer
        extension with no line at all is refused, because nothing could dial it.
        """
        requested = str(data.get("extension", "")).strip()
        digits = extension_digits(requested)
        if not digits.isdigit() or not 100 <= int(digits) <= 999:
            raise ValueError("Extension must be a 3-digit number from 100 to 999")
        number_text = extension_scope(requested) or str(data.get("number") or "").strip()
        number_row = None
        if number_text:
            number_row = self.extension_number_row(number_text)
            # The number decides the line; the digits decide the device on it.
            # Both are checked, so `101` on a number that already holds one, or
            # on somebody else's number, never becomes a second identity.
            if number_row is None:
                raise ValueError("Extension must belong to one of this account's numbers")
            if number_row["owner_user_id"] is not None and not same_account(number_row["owner_user_id"], owner_user_id):
                raise ValueError("Extension must belong to one of this account's numbers")
            existing = self.resolve_extension(str(number_row["number"]), digits, owner_user_id)
        else:
            matches = [row for row in self.list_extensions(owner_user_id) if row["digits"] == digits]
            if len(matches) > 1:
                raise ValueError(
                    f"{digits} is on more than one of this account's numbers - "
                    "choose the extension on the number it belongs to"
                )
            existing = matches[0] if matches else None
            if existing is None and owner_user_id is not None:
                # A row with no number of its own is named by its digits alone,
                # so that identity cannot belong to two accounts: the account
                # that holds it is named instead - unless the request is the
                # deliberate move of that very row, which is what `reassign`
                # asks for, in which case it is the row being edited.
                held = next(
                    (row for row in self.list_extensions()
                     if not row["number"] and row["digits"] == digits
                     and not same_account(row.get("owner_user_id"), owner_user_id)),
                    None,
                )
                if held is not None and bool(data.get("reassign")):
                    existing = held
                elif held is not None:
                    raise ValueError(
                        f"Extension {digits} already belongs to {self._account_label(held.get('owner_user_id'))}. "
                        "Choose a free extension number, or name the phone number this device belongs to."
                    )
                # A customer's extension belongs to one of their numbers: three
                # digits are resolved inside the current number, so a row with no
                # line answers nothing. While the account has no numbers at all
                # there is nothing to choose, so the row is created waiting for
                # the first line - saving a number adopts it, and the console
                # shows it as "No number yet". Once the account holds a number,
                # that number has to be named rather than guessed.
                if any(row["number"] for row in self.list_extensions(owner_user_id)):
                    raise ValueError(
                        "Choose the phone number this extension belongs to: "
                        "a three-digit extension is resolved inside the current phone number"
                    )
        extension = str(existing["key"]) if existing is not None else extension_key(digits, number_text)
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
            row = db.execute(
                "SELECT id,sip_username,sip_password_enc,voicemail_enabled,voicemail_pin_enc,owner_user_id,phone_number_id "
                "FROM extensions WHERE id=?", (int(existing["id"]),)
            ).fetchone() if existing is not None else None
            created = row is None
            # Who this extension belongs to, and who is being asked to own it. A
            # request that says nothing about the owner keeps the one the row has,
            # so an edit can neither take a line from another account nor orphan
            # one into the platform's dial plan.
            if owner_user_id is not None:
                new_owner = int(owner_user_id)
            elif "owner_user_id" in data:
                new_owner = int(data["owner_user_id"]) if str(data.get("owner_user_id", "")).isdigit() else None
            else:
                new_owner = row["owner_user_id"] if row is not None else None
            if row is not None and enforce_owner and owner_user_id is not None and not same_account(row["owner_user_id"], owner_user_id):
                raise ValueError("Extension belongs to another customer")
            # An extension is never handed to a different account by creating a
            # line on a number; moving a live one is a deliberate act - the
            # console asks for it, editing that very extension and its owner.
            if (
                row is not None and row["owner_user_id"] is not None
                and not same_account(row["owner_user_id"], new_owner)
                and not bool(data.get("reassign"))
            ):
                holder = self._account_label(row["owner_user_id"])
                raise ValueError(
                    f"Extension {digits} already belongs to {holder}. "
                    "Choose a free extension number, or edit that extension to move it."
                )
            target_id = int(number_row["id"]) if number_row is not None else None
            if row is not None and (target_id or row["phone_number_id"]) and target_id != row["phone_number_id"]:
                # Moving an extension to another line must not land on top of an
                # extension that is already there.
                clash = db.execute(
                    "SELECT id FROM extensions WHERE extension=? AND phone_number_id=?",
                    (digits, target_id),
                ).fetchone() if target_id is not None else None
                if clash:
                    raise ValueError(f"Extension {digits} is already on that phone number")
            # Devices authenticate with this name, so it is derived here and never
            # accepted from a caller: the account, the extension and the line it
            # answers on. Editing keeps the identity a registered phone already
            # uses, so no handset has to be reconfigured. It is a technical name -
            # nobody ever dials it.
            username = self.canonical_sip_username(
                digits, row["sip_username"] if row is not None else "", number_text,
                self._account_label(new_owner) if new_owner is not None else "",
            )
            voicemail_enabled = bool(voicemail_enabled_value) if voicemail_enabled_value is not None else bool(row and row["voicemail_enabled"])
            # A new extension provisions its own SIP credentials, so a customer
            # can create an extension and register a device without inventing a
            # password first. An explicit password always wins, and editing
            # without one keeps the existing secret.
            generated = False
            if password:
                encrypted = self.encrypt(password)
            elif row is not None:
                encrypted = row["sip_password_enc"]
            else:
                encrypted = self.encrypt(self.generate_sip_password())
                generated = True
            voicemail_pin_enc = self.encrypt(voicemail_pin) if voicemail_pin else (row["voicemail_pin_enc"] if row is not None else "")
            if voicemail_enabled and not voicemail_pin_enc:
                raise ValueError("Voicemail PIN is required when voicemail is enabled")
            if created:
                db.execute(
                    "INSERT INTO extensions(extension,phone_number_id,display_name,sip_username,sip_password_enc,"
                    "webrtc_enabled,recording_enabled,voicemail_enabled,voicemail_pin_enc,voicemail_email,active,owner_user_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        digits, target_id, str(data.get("display_name", "")).strip()[:120], username, encrypted,
                        int(bool(data.get("webrtc_enabled"))), int(bool(data.get("recording_enabled", False))),
                        int(voicemail_enabled), voicemail_pin_enc, voicemail_email,
                        int(bool(data.get("active", True))), new_owner,
                    ),
                )
            else:
                db.execute(
                    "UPDATE extensions SET extension=?,phone_number_id=?,display_name=?,sip_username=?,sip_password_enc=?,"
                    "webrtc_enabled=?,recording_enabled=?,voicemail_enabled=?,voicemail_pin_enc=?,voicemail_email=?,"
                    "active=?,owner_user_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (
                        digits, target_id, str(data.get("display_name", "")).strip()[:120], username, encrypted,
                        int(bool(data.get("webrtc_enabled"))), int(bool(data.get("recording_enabled", False))),
                        int(voicemail_enabled), voicemail_pin_enc, voicemail_email,
                        int(bool(data.get("active", True))), new_owner, row["id"],
                    ),
                )
        owner = new_owner
        # Every extension gets a working call flow of its own. It is only written
        # when the extension is created, so a flow the customer later edits is
        # never overwritten.
        if created and owner is not None and int(bool(data.get("active", True))):
            self.ensure_extension_flow(int(owner), extension, voicemail=bool(voicemail_enabled))
            self.sync_primary_flows(int(owner), extension)
        return extension

    def primary_extension(self, owner_user_id: int, number: str = "") -> str:
        """The first device of a line, by key: 101 on that number.

        With a number - or without one, in which case the account's main line is
        used - this is that line's lowest active extension, the one
        auto-provisioning created when the number was assigned. The key is
        returned rather than the digits because two lines' 101s have to be told
        apart wherever this is compared.
        """
        scope = str(number or "").strip() or self.primary_number(owner_user_id)
        if not scope:
            return ""
        rows = self.extensions_on_number(owner_user_id, scope)
        keys = sorted(
            (str(row["key"]) for row in rows if row["active"] and row["digits"].isdigit()),
            key=lambda value: int(extension_digits(value) or 0),
        )
        return keys[0] if keys else ""

    def sync_primary_flows(self, owner_user_id: int, extension: str) -> None:
        """A line rings every device on it, so adding one extends the line.

        Only a flow that is still the generated one is rewritten: a single ring
        step, the default timeout, no group, and members that are a subset of the
        line's own devices. Anything the customer has designed - extra steps,
        another timeout, a group - is left exactly as it is.
        """
        owner_user_id, extension = int(owner_user_id), str(extension)
        # Only a line the extension belongs to is rewritten: the number in its
        # key, or the account's main line for a row that is still being placed.
        primary_number = extension_scope(extension) or self.primary_number(owner_user_id)
        if not primary_number:
            return
        devices = self.line_devices(owner_user_id, primary_number)
        if len(devices) < 2 or extension not in devices:
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
        """The customer's main line.

        The number the account's default-outbound flag marks, else the first of
        its numbers that has extensions on it, else its oldest number - so a
        fresh number with no extensions yet still has a main line.
        """
        owner_user_id = int(owner_user_id)
        numbers = [row for row in self.list_numbers(owner_user_id) if row["active"]]
        # The flagged line first - the account has one main line - then the
        # lowest line that has extensions, then the lowest line at all, so a
        # brand new number still answers for an extension added before it.
        for row in numbers:
            if row["default_outbound"]:
                return str(row["number"])
        for row in numbers:
            if self.extensions_on_number(owner_user_id, row["number"]):
                return str(row["number"])
        return str(numbers[0]["number"]) if numbers else ""

    # The identity a device authenticates with. It is a technical name: a phone
    # signs in with it, nobody ever dials it, and it is unique across the whole
    # platform because two customers - even two numbers of one customer - may
    # both have an extension 101. New usernames carry all three parts
    # (`MERIDIAN_101_13025550001`); a name already minted in an older shape
    # (`KUDGTE_101`) is kept as-is, because renaming it would stop the phone
    # that holds it from registering until somebody reconfigured the handset.
    SIP_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,79}$")

    @classmethod
    def generate_sip_username(cls, extension: str, number: Any = "", label: str = "") -> str:
        """`MERIDIAN_101_13025550001` - the account, the extension, its line."""
        digits = extension_digits(extension) or "100"
        stem = re.sub(r"[^A-Za-z0-9]", "", str(label or "")).upper()[:24]
        if not stem:
            stem = "".join(secrets.choice(string.ascii_uppercase) for _ in range(6))
        parts = [stem, digits, re.sub(r"[^0-9]", "", str(number or ""))]
        return "_".join(part for part in parts if part)

    @classmethod
    def canonical_sip_username(cls, extension: str, current: str = "", number: Any = "", label: str = "") -> str:
        """Keep an identity that already names this extension, mint one otherwise.

        Editing an extension must not silently change the identity a phone logs in
        with, so a canonical value is reused; anything else (a row from before this
        rule, a caller-supplied name) is replaced.
        """
        current = str(current or "")
        digits = extension_digits(extension)
        if cls.SIP_USERNAME_RE.match(current) and re.search(rf"_{re.escape(digits)}(_\d+)?$", current):
            return current
        return cls.generate_sip_username(digits, number, label)

    def normalise_sip_usernames(self, db=None) -> int:
        """Give every extension the canonical SIP identity, once, at startup.

        Linked device accounts inherit the identity of the extension they register
        for, so the two never disagree in the generated Asterisk configuration.
        """
        owned = db is None
        if owned:
            db = self._connect()
        try:
            rows = db.execute(
                "SELECT e.id,e.extension,e.sip_username,n.number AS number,u.username AS owner_name FROM extensions e "
                "LEFT JOIN phone_numbers n ON n.id=e.phone_number_id "
                "LEFT JOIN admin_users u ON u.id=e.owner_user_id"
            ).fetchall()
            changed = 0
            for row in rows:
                username = self.canonical_sip_username(
                    extension_key(row["extension"], row["number"] or ""), row["sip_username"],
                    row["number"] or "", row["owner_name"] or "",
                )
                if username == str(row["sip_username"] or ""):
                    continue
                db.execute("UPDATE extensions SET sip_username=? WHERE id=?", (username, row["id"]))
                # A device account names the key that says which line's extension
                # it signs in as, or the digits a legacy row held while they
                # pinned one line through its own `phone_number`.
                key = extension_key(str(row["extension"]), row["number"] or "")
                db.execute(
                    "UPDATE customer_sip_accounts SET sip_username=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE extension=? OR (extension=? AND phone_number=?)",
                    (username, key, str(row["extension"]), row["number"] or ""),
                )
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

    def next_extension_number(self, owner_user_id: int | None = None, number: str = "") -> str:
        """Lowest free three-digit extension on this number, starting at 101.

        Every phone number has its own extension set - 101 is where a line
        starts - so 101 is free again on a number that has none, whatever other
        accounts (or another of this account's numbers) are using. Without a
        number the whole account's set is considered, which is what a caller that
        predates per-number extensions gets: nothing is offered twice.
        """
        scope = str(number or "").strip()
        with self._connect() as db:
            if scope:
                rows = db.execute(
                    "SELECT extension FROM extensions WHERE phone_number_id="
                    "(SELECT id FROM phone_numbers WHERE number=?)",
                    (scope,),
                ).fetchall()
                taken = {extension_digits(row["extension"]) for row in rows}
            else:
                rows = db.execute("SELECT extension,phone_number_id FROM extensions").fetchall()
                taken = {
                    extension_digits(row["extension"]) for row in rows
                    if row["phone_number_id"] in (None, "")
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

        Two administrator-set addresses, because they travel different paths:
        `service_host` is the SIP address phones register with (a DNS record
        pointing straight at this server - SIP/UDP cannot go through a web
        proxy), and `service_web_host` is the domain the consoles, API and
        webhook examples are served from (typically behind a reverse proxy,
        e.g. tel.example.com). When only one is set it serves both roles, so
        an existing single-address deployment keeps working unchanged.
        """
        settings = self.get_settings()
        host = str(settings.get("service_host") or "").strip()
        web_host = str(settings.get("service_web_host") or "").strip()
        configured = bool(host)
        web_configured = bool(web_host)
        if not host:
            host = web_host or str(fallback_host or "").strip()
        if not web_host:
            web_host = str(settings.get("service_host") or "").strip() or str(fallback_host or "").strip()
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
            "web_host": web_host,
            "web_configured": web_configured,
            "sip": f"{host}:{port}" if host else "",
            "api_base": f"https://{web_host}" if web_host else "",
        }

    def reveal_extension_credentials(self, extension, owner_user_id: int | None = None, fallback_host: str = ""):
        """The SIP credentials a device registers with, plus where to register.

        A device account linked to the extension overrides the extension's own
        secret in the generated Asterisk config, so the effective credential is
        what gets returned.
        """
        row = self._extension_row(extension)
        if not row or (owner_user_id is not None and not same_account(row["owner_user_id"], owner_user_id)):
            raise ValueError("Extension not found")
        owner = row["owner_user_id"]
        digits, scope = str(row["digits"]), str(row["number"])
        # A customer's sheet lists their own numbers; a platform-owned
        # extension (no customer) still lists the platform-owned DIDs that
        # point at it instead of always claiming none are assigned. A number
        # rings the extension of its own scope, which is why a scoped extension
        # can only be answered by the line it belongs to.
        numbers = [
            item for item in self.list_numbers(owner)
            # The link may be the key (`101@+1302...`) or the bare digits of an
            # older deployment; a scoped extension is only answered by its line.
            if extension_digits(item["inbound_extension"]) == digits and item["active"]
            and (not scope or str(item["number"]) == scope)
            and (owner is not None or item.get("owner_user_id") is None)
        ]
        device = device_account_for_extension(row, self.list_sip_accounts(owner, include_password=True))
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
            "extension": digits,
            "key": row["key"],
            "id": row["id"],
            "digits": digits,
            "number": scope,
            "mailbox": row["mailbox"],
            "display_name": row["display_name"],
            "active": bool(row["active"]),
            "sip_username": device["sip_username"] if device else (row["sip_username"] or row["key"]),
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
        """Turn one device's recording switch over, on the exact row it names.

        `extension` is the key (`101@+13025550001`), or the digits while one
        extension of the platform carries them; the row is then updated by its
        primary key, because the digits alone are not unique any more.
        """
        row = self._extension_row(str(extension).strip())
        if not row or not row["active"]:
            raise ValueError("Active extension not found")
        with self._connect() as db:
            db.execute(
                "UPDATE extensions SET recording_enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (int(bool(enabled)), int(row["id"])),
            )

    def _assign_extension_to_number(self, extension_id: int, number: str) -> bool:
        """Put one existing extension on this number, unless the digits are taken."""
        with self._connect() as db:
            target = db.execute("SELECT id FROM phone_numbers WHERE number=?", (str(number),)).fetchone()
            row = db.execute("SELECT extension,phone_number_id FROM extensions WHERE id=?", (int(extension_id),)).fetchone()
            if target is None or row is None:
                return False
            if row["phone_number_id"] == target["id"]:
                return True
            clash = db.execute(
                "SELECT 1 FROM extensions WHERE extension=? AND phone_number_id=?",
                (str(row["extension"]), target["id"]),
            ).fetchone()
            if clash:
                return False
            db.execute(
                "UPDATE extensions SET phone_number_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (target["id"], int(extension_id)),
            )
        self.remap_extension_references()
        return True

    def _extension_free_on(self, number_id: int, digits: str) -> bool:
        """Is nothing of this line already answering those digits?"""
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM extensions WHERE extension=? AND phone_number_id=?",
                (str(digits), int(number_id)),
            ).fetchone()
        return row is None

    def _adopt_unassigned_extensions(self, owner_user_id: int | None, number: str) -> int:
        """Put the account's number-less extensions on its first line.

        A row can have no line because it was written before extensions belonged
        to a number, or because an API caller named only the digits. It answers
        nothing while it has none - three-digit dialling is resolved inside the
        current phone number - so the account's first number takes them, which is
        what keeps every device an install already had reachable. A row whose
        digits the line already holds is left alone and reported, never merged.
        """
        if owner_user_id is None:
            return 0
        with self._connect() as db:
            first = db.execute(
                "SELECT id FROM phone_numbers WHERE owner_user_id=? ORDER BY id LIMIT 1", (int(owner_user_id),)
            ).fetchone()
            target = db.execute("SELECT id FROM phone_numbers WHERE number=?", (str(number),)).fetchone()
            if first is None or target is None or int(first["id"]) != int(target["id"]):
                return 0
            taken = {
                extension_digits(row["extension"])
                for row in db.execute("SELECT extension FROM extensions WHERE phone_number_id=?", (target["id"],)).fetchall()
            }
            rows = db.execute(
                "SELECT id,extension FROM extensions WHERE phone_number_id IS NULL AND owner_user_id=? ORDER BY id",
                (int(owner_user_id),),
            ).fetchall()
            moved = 0
            for row in rows:
                digits = extension_digits(row["extension"])
                if not digits.isdigit() or digits in taken:
                    continue
                db.execute(
                    "UPDATE extensions SET phone_number_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (target["id"], row["id"]),
                )
                taken.add(digits)
                moved += 1
        if moved:
            self.remap_extension_references()
        return moved

    def delete_extension(self, extension, owner_user_id: int | None = None):
        """Remove one extension, and every reference to it.

        Extension numbers repeat across numbers and customers, so what is deleted
        is the exact identity: deleting 101 on one number leaves the 101 on the
        other, and every other customer's 101, untouched. A number that still
        points at the extension has to be re-pointed first, because that link is
        what makes an inbound call ring.
        """
        key = str(extension or "").strip()
        digits, scope = extension_digits(key), extension_scope(key)
        if not scope and digits.isdigit():
            # Digits name one extension while exactly one carries them; where
            # several do, the console sends the number as well.
            row = self._extension_row(digits)
            if row is None:
                raise ValueError("Extension not found")
            key = str(row["key"])
        with self._connect() as db:
            if scope:
                found = db.execute(
                    "SELECT id,owner_user_id FROM extensions WHERE extension=? AND phone_number_id="
                    "(SELECT id FROM phone_numbers WHERE number=?)",
                    (digits, scope),
                ).fetchone()
            else:
                found = db.execute(
                    "SELECT id,owner_user_id FROM extensions WHERE extension=? AND phone_number_id IS NULL",
                    (digits,),
                ).fetchone()
            if found is None or (
                owner_user_id is not None and not same_account(found["owner_user_id"], owner_user_id)
            ):
                raise ValueError("Extension not found")
            pointing = next(
                (
                    row["number"] for row in self.list_numbers()
                    if self._link_is(row["inbound_extension"], key, found["owner_user_id"])
                ),
                None,
            )
            if pointing:
                raise ValueError(f"Cannot delete an extension used by inbound number {pointing}")
            if self.extension_is_a_call_default(key):
                raise ValueError("Cannot delete an extension used as a call default; change Call defaults first")
            db.execute("DELETE FROM extensions WHERE id=?", (found["id"],))
            # A device keyed to the extension keeps its registration but is no
            # longer keyed to a device, and no group or flow points at nothing.
            db.execute(
                "UPDATE customer_sip_accounts SET extension='',updated_at=CURRENT_TIMESTAMP WHERE extension=?",
                (digits,),
            )
            for group in db.execute("SELECT id,members FROM extension_groups").fetchall():
                members = [ext for ext in str(group["members"] or "").split(",") if ext and ext != key]
                if len(members) != len(str(group["members"] or "").split(",")):
                    db.execute(
                        "UPDATE extension_groups SET members=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (",".join(members), group["id"]),
                    )
            db.execute("DELETE FROM routing_flows WHERE target_type='extension' AND target=?", (key,))

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
        """Create or edit one phone number, with the extension its calls ring."""
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
        claimed = None
        if inbound:
            # The link names one extension of this very number - `101` is this
            # line's 101 - and it is stored as its key so a later 101 elsewhere
            # cannot be confused with it.
            wanted = extension_digits(inbound)
            scope = extension_scope(inbound)
            if scope and scope != number:
                raise ValueError("Inbound extension belongs to another number")
            linked = self.resolve_extension(number, wanted, owner_user_id)
            if linked is None:
                # The link may name this customer's own extension that has no
                # line yet: the number the link names is the number that claims
                # it, which is exactly how a legacy install is migrated.
                candidates = [
                    row for row in self.unassigned_extensions(owner_user_id)
                    if row["digits"] == wanted and (owner_user_id is not None or row.get("owner_user_id") in (None, ""))
                ]
                if len(candidates) == 1:
                    claimed = linked = candidates[0]
                    line = self.extension_number_row(number)
                    if line is not None and not self._extension_free_on(int(line["id"]), wanted):
                        # The row has no line of its own and this line already
                        # answers those digits: claiming it would silently merge
                        # two devices, so nothing is written.
                        claimed = linked = None
                        raise ValueError(
                            f"Extension {wanted} is already used on {number} - choose a free extension number"
                        )
            if linked is not None:
                if not linked["active"] or (
                    linked["owner_user_id"] is not None and not same_account(linked["owner_user_id"], owner_user_id)
                ):
                    raise ValueError("Inbound extension must belong to the selected customer")
                inbound = str(linked["key"])
            else:
                # The extension is not on this line - it may be another number of
                # the same account, another customer's device, or not created yet.
                # The link stays inside this number: it is stored as this line's
                # own key and answers nothing - NOT IN SERVICE - until an
                # extension with those digits exists on this very number. Nothing
                # is borrowed from another line or another account.
                inbound = extension_key(wanted, number)
        with self._connect() as db:
            if owner_user_id and not db.execute("SELECT 1 FROM admin_users WHERE id=? AND role='user' AND active=1", (owner_user_id,)).fetchone():
                raise ValueError("Customer account is not active")
            if data.get("provider") and not db.execute(
                "SELECT 1 FROM sip_providers WHERE name=? AND active=1", (str(data.get("provider")).strip(),)
            ).fetchone():
                raise ValueError("Provider must be an active configured SIP provider")
            default_outbound = bool(data.get("default_outbound"))
            if default_outbound and not inbound:
                raise ValueError("A default outbound number must be assigned to an extension")
            if default_outbound:
                db.execute(
                    "UPDATE phone_numbers SET default_outbound=0,updated_at=CURRENT_TIMESTAMP WHERE inbound_extension=?",
                    (inbound,),
                )
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
                    inbound[:64],
                    int(default_outbound),
                    int(bool(data.get("active", True))),
                    owner_user_id,
                    price_cents,
                    str(data.get("billing_start", "")).strip()[:10],
                    cycle_day,
                    str(data.get("discontinue_at", "")).strip()[:10],
                ),
            )
        if claimed is not None and not self._assign_extension_to_number(int(claimed["id"]), number):
            raise ValueError(
                f"Extension {extension_digits(claimed.get('extension'))} is already used on {number} - "
                "choose a free extension number"
            )
        self._adopt_unassigned_extensions(owner_user_id, number)
        return number

    def active_number(self, number: str):
        """One active phone number of the platform, by its E.164 text."""
        with self._connect() as db:
            row = db.execute(
                "SELECT number,provider,owner_user_id FROM phone_numbers WHERE number=? AND active=1",
                (str(number or "").strip(),),
            ).fetchone()
        return dict(row) if row else None

    def get_outbound_number(self, extension: str, requested: str | None = None):
        """The line an extension calls out on: the phone number it belongs to.

        Caller ID comes from the phone-number context, not from the extension:
        101 on +13025550001 presents that line and 101 on +13025550002 presents
        the other one, whichever handset is registered to either. There is no
        account-level fallback - an extension that has no line of its own has
        nothing to present, and the call is refused instead of borrowing the
        account's main line. A caller who wants a particular line names it, and
        that number has to be active and one of *this account's*, so nothing can
        ever present another customer's number. The platform's own devices (no
        customer, no number) present the line whose link names them.
        """
        row = self._extension_row(extension)
        if row is None:
            return None
        own_number = str(row.get("number") or "")
        owner = row.get("owner_user_id")
        with self._connect() as db:
            if own_number and not requested:
                found = db.execute(
                    "SELECT number,provider FROM phone_numbers WHERE number=? AND active=1", (own_number,)
                ).fetchone()
                return dict(found) if found else None
            if requested:
                found = db.execute(
                    "SELECT number,provider FROM phone_numbers WHERE number=? AND active=1 AND "
                    "(owner_user_id=? OR (owner_user_id IS NULL AND ? IS NULL))",
                    (requested, owner, owner),
                ).fetchone()
                return dict(found) if found else None
            if owner is None:
                found = db.execute(
                    "SELECT n.number,n.provider FROM phone_numbers n WHERE n.active=1 AND n.inbound_extension=? "
                    "ORDER BY n.default_outbound DESC,n.id LIMIT 1",
                    (str(row["key"]),),
                ).fetchone()
                return dict(found) if found else None
        # No line of its own and no line named: nothing is presented, and the
        # dial plan's OUTBOUND_TRUNK stays empty, so the call is politely refused.
        return None

    def set_default_outbound_number(self, extension: str, number: str):
        """Mark one of the account's lines as the number its calls present.

        The caller ID belongs to the phone number, so the flag is asserted on the
        line itself: an extension may only point at a number that is its own line
        or one of its account's, and the account's previous default is cleared.
        """
        row = self._extension_row(extension)
        if row is None:
            raise ValueError("Extension not found")
        owner = row.get("owner_user_id")
        with self._connect() as db:
            target = db.execute(
                "SELECT number,owner_user_id FROM phone_numbers WHERE number=? AND active=1", (str(number),)
            ).fetchone()
            if target is None or not same_account(target["owner_user_id"], owner):
                raise ValueError("Number is not assigned to this extension")
            if row.get("number") and str(row["number"]) != str(target["number"]):
                raise ValueError("Number is not assigned to this extension")
            if owner is None:
                db.execute("UPDATE phone_numbers SET default_outbound=0,updated_at=CURRENT_TIMESTAMP WHERE owner_user_id IS NULL")
            else:
                db.execute(
                    "UPDATE phone_numbers SET default_outbound=0,updated_at=CURRENT_TIMESTAMP WHERE owner_user_id=?",
                    (int(owner),),
                )
            db.execute("UPDATE phone_numbers SET default_outbound=1,updated_at=CURRENT_TIMESTAMP WHERE number=?", (str(number),))

    def delete_number(self, number, cascade: bool = True):
        """Remove a number and, by default, the line provisioned for it.

        A number carries its own extension set, so deleting it takes that set down
        with it - unless another number still rings one of those extensions, or an
        extension is a customer's call default. Extensions of other numbers, and
        other customers' extensions with the same digits, are never touched.
        """
        number = str(number).strip()
        with self._connect() as db:
            row = db.execute(
                "SELECT number,inbound_extension,owner_user_id FROM phone_numbers WHERE number=?", (number,)
            ).fetchone()
            if not row:
                raise ValueError("Phone number not found")
            # The rows are captured by id *before* the number goes: after that
            # their (digits, number) lookup no longer finds them, and an extension
            # that survived its own line would become a numberless row whose bare
            # digits are ambiguous.
            line = [
                (int(item["id"]), str(item["key"])) for item in self.list_extensions()
                if item["number"] == number
            ]
            db.execute("DELETE FROM phone_numbers WHERE number=?", (number,))
            db.execute("DELETE FROM call_routes WHERE phone_number=?", (number,))
        if not cascade:
            # The extensions stay, with no line: unreachable until an operator
            # puts them on a number, and visible as such in the console.
            with self._connect() as db:
                db.execute(
                    "UPDATE extensions SET phone_number_id=NULL,updated_at=CURRENT_TIMESTAMP "
                    "WHERE phone_number_id IS NULL OR phone_number_id NOT IN (SELECT id FROM phone_numbers)"
                )
            return
        for extension_id, key in line:
            digits = extension_digits(key)
            with self._connect() as db:
                # Another line keeping this extension alive would have to name it
                # by its key: the digits alone mean a different 101 on a different
                # line now, so another line's own `101@<its number>` link is not a
                # reference to this device.
                still_linked = db.execute(
                    "SELECT 1 FROM phone_numbers WHERE inbound_extension=?", (key,)
                ).fetchone()
                if still_linked:
                    continue
                # A device account names this key, or the digits while its own
                # number pinned the line - and that number is the one going away.
                db.execute(
                    "DELETE FROM customer_sip_accounts WHERE extension=? OR (extension=? AND phone_number=?)",
                    (key, digits, number),
                )
            self._drop_extension_from_call_defaults(key)
            if self.extension_is_a_call_default(key):
                # Still load-bearing: leave it in place rather than break the
                # account's call defaults.
                continue
            self._delete_extension_by_id(extension_id, key)
        self._release_dangling_extensions()

    def _delete_extension_by_id(self, extension_id: int, key: str) -> None:
        """Delete one extension row by its id, with every reference to it.

        Used while a line is being taken down: the row has to be found by id,
        because the number it answered on is already gone and (digits, number) no
        longer names it. The checks the public `delete_extension()` makes - a link
        still pointing at it, a customer's call default - are the caller's, and
        the row's own key is what a flow or a group names.
        """
        with self._connect() as db:
            if db.execute("SELECT 1 FROM extensions WHERE id=?", (int(extension_id),)).fetchone() is None:
                return
            db.execute("DELETE FROM extensions WHERE id=?", (int(extension_id),))
            for group in db.execute("SELECT id,members FROM extension_groups").fetchall():
                members = [ext for ext in str(group["members"] or "").split(",") if ext and ext != key]
                if len(members) != len(str(group["members"] or "").split(",")):
                    db.execute(
                        "UPDATE extension_groups SET members=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (",".join(members), group["id"]),
                    )
            db.execute("DELETE FROM routing_flows WHERE target_type='extension' AND target=?", (key,))

    def _release_dangling_extensions(self) -> None:
        """No row may keep pointing at a phone number that no longer exists.

        A row that survived its line - because another link names it, or a call
        default still uses it - would otherwise read as a numberless row under
        its *bare digits*: a second identity for the digits that is not a device
        anywhere. Released, it is plainly an extension with no line, which is
        what the number column then honestly shows.
        """
        with self._connect() as db:
            db.execute(
                "UPDATE extensions SET phone_number_id=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE phone_number_id IS NOT NULL AND phone_number_id NOT IN (SELECT id FROM phone_numbers)"
            )

    def _drop_extension_from_call_defaults(self, extension: str) -> None:
        """Clear customer call-default entries that point at this extension."""
        extension = str(extension)
        try:
            stored = json.loads(self.get_settings().get(self.CALL_DEFAULTS_KEY) or "{}")
        except (TypeError, ValueError):
            return
        changed = False
        for entry in stored.values():
            if not isinstance(entry, dict):
                continue
            for field in ("outbound", "fallback"):
                value = str(entry.get(field) or "")
                # The key only. Clearing every bare `101` while one line is
                # taken down would silently drop another line's default - a
                # different device that happens to read the same. A row with no
                # number of its own is named by its digits, which *is* its key.
                if value and value == extension:
                    entry[field] = ""
                    changed = True
        if changed:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO settings(`key`,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                    (self.CALL_DEFAULTS_KEY, json.dumps(stored)),
                )

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
            rows = db.execute(f"SELECT id,owner_user_id,label,sip_username,sip_password_enc,server,port,transport,phone_number,extension,registration_status,last_registered_at,active,created_at,updated_at FROM customer_sip_accounts{where}", (int(owner_user_id),) if owner_user_id is not None else ()).fetchall()
        # Extensions first, grouped by the number they belong to and in dialling
        # order; a device that is not keyed to one keeps its place at the end.
        rows = sorted(rows, key=lambda row: (
            0 if extension_digits(row["extension"]) else 1,
            str(row["extension"]),
            int(extension_digits(row["extension"]) or 0),
            str(row["label"]),
        ))
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
        digits = extension_digits(extension)
        if extension:
            # A device keyed to an extension *is* that extension's generated
            # identity, and the key is what the row stores so two lines' 101s
            # never look like the same device.
            row = next((item for item in extensions if item["key"] == extension), None)
            wanted_number = str(data.get("phone_number") or "").strip()
            if row is None and extension.isdigit() and not extension_scope(extension):
                # The console may name the digits a person reads. The line the
                # device is being put on answers first, and only a single
                # remaining candidate is unambiguous: two 101s have to be named
                # with their number.
                candidates = [item for item in extensions if item["digits"] == digits]
                if wanted_number:
                    named = [item for item in candidates if item["number"] == wanted_number]
                    candidates = named or candidates
                row = candidates[0] if len(candidates) == 1 else None
            if not row:
                raise ValueError("SIP extension is not assigned to this customer")
            extension = str(row["key"])
            username = str(row["sip_username"] or "") or self.generate_sip_username(
                digits, row["number"], self._account_label(row.get("owner_user_id"))
            )
        else:
            username = self._validate_config_value(data.get("sip_username"), "SIP username", 100)
            # A name an extension answers to is never handed to a device: a
            # three-digit username would collide with the PJSIP endpoint name the
            # extension's own phones are reached by.
            taken = {str(row["sip_username"]) for row in extensions}
            if username in taken or username.isdigit():
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
        if extension:
            # The device is keyed to one extension, named by its key or - while
            # only one carries them - by the digits it is dialled with.
            digits, scope = extension_digits(extension), extension_scope(extension)
            match = next(
                (row for row in self.list_extensions(owner_user_id)
                 if row["digits"] == digits and (not scope or row["number"] == scope)),
                None,
            )
            if match is None:
                raise ValueError("SIP extension is not assigned to this customer")
            extension = str(match["key"])
        if extension and phone_number:
            # A device is a phone on one line, so the number it belongs to and
            # the extension it answers have to be the same line.
            scope = extension_scope(extension)
            if scope and scope != phone_number:
                raise ValueError("SIP number and extension belong to different numbers")
            if not scope:
                linked = next((row for row in self.list_numbers(owner_user_id) if str(row["inbound_extension"] or "") == digits), None)
                phone_number = phone_number or ((linked or {}).get("number") or "")
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
        row = self._extension_row(str(extension).strip())
        if not row or (owner_user_id is not None and not same_account(row["owner_user_id"], owner_user_id)):
            raise ValueError("Extension not found")
        extension = str(row["key"])
        if password and any(char in password for char in "\r\n;#"):
            raise ValueError("Invalid SIP password")
        secret = password or self.generate_sip_password()
        owner = row["owner_user_id"]
        device = (
            device_account_for_extension(row, self.list_sip_accounts(owner, include_password=True))
            if owner is not None else None
        )
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
                    "UPDATE extensions SET sip_password_enc=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (self.encrypt(secret), int(row["id"])),
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
        "after_hours", "extension", "voicemail", "forward", "ivr",
    }

    # The menu a caller can be given, and the sound each voice plays. A voice is
    # a recorded prompt set: the media below must exist on the deployment (or be
    # produced by whatever text-to-speech engine it runs), and the console shows
    # the operator's own text beside it whatever the audio was made from.
    IVR_DEFAULT_PROMPT = "Welcome to EngineerIP. Please enter the extension you wish to reach."
    IVR_VOICES = (
        {"id": "platform", "label": "English (US) - platform voice", "media": "custom/ivr-welcome"},
        {"id": "en-gb", "label": "English (UK)", "media": "custom/ivr-welcome-en-gb"},
        {"id": "es-us", "label": "Espanol (US)", "media": "custom/ivr-welcome-es-us"},
        {"id": "fr-ca", "label": "Francais (Canada)", "media": "custom/ivr-welcome-fr-ca"},
    )
    # One extension has nothing to choose from; more than five is a directory.
    IVR_AUTO_ABOVE = 5

    def ivr_voices(self) -> list[dict]:
        return [dict(row) for row in self.IVR_VOICES]

    def ivr_voice(self, voice_id: str) -> dict:
        return next((dict(row) for row in self.IVR_VOICES if row["id"] == str(voice_id)), dict(self.IVR_VOICES[0]))

    @classmethod
    def auto_ivr_wanted(cls, active_extensions: int) -> bool:
        """The operator's rule: never with a single extension, always past five,
        and the customer's own decision in between."""
        return int(active_extensions) > cls.IVR_AUTO_ABOVE

    def default_ivr_node(self) -> dict:
        return {
            "type": "ivr", "prompt": self.IVR_DEFAULT_PROMPT, "voice": "platform",
            "input_timeout": 6, "attempts": 2, "fallback": "",
            "label": "Enter an extension", "configured": True,
        }

    def _validate_route_nodes(self, route: dict, owner_user_id: int, target_type: str = "number", target: str = "") -> None:
        """One validator for every kind of call flow.

        Ring destinations must be extensions the customer owns, and a step that
        names a saved group must belong to that customer with members drawn from
        the group, so a flow can never ring somebody else's phone. A number's own
        flow is stricter: 101 means that line's 101, so it may only ring the
        extensions of that number - which is also what makes the routing canvas
        show one line's set at a time.
        """
        if not isinstance(route, dict) or not isinstance(route.get("nodes"), list) or len(route["nodes"]) > 100:
            raise ValueError("Call route must contain a nodes array with at most 100 nodes")
        # The step is usable the moment it is dropped in: text, voice, waits and
        # attempts all have a working default, and editing is what changes them.
        for node in route["nodes"]:
            if isinstance(node, dict) and node.get("type") == "ivr":
                default = self.default_ivr_node()
                node["prompt"] = str(node.get("prompt") or "").strip()[:400] or default["prompt"]
                node["voice"] = str(node.get("voice") or default["voice"])
                node["input_timeout"] = int(node.get("input_timeout") or default["input_timeout"])
                node["attempts"] = int(node.get("attempts") or default["attempts"])
                node["fallback"] = str(node.get("fallback") or "")
                node["label"] = str(node.get("label") or default["label"])[:120]
        if any(not isinstance(node, dict) or node.get("type") not in self.ROUTE_NODE_TYPES for node in route["nodes"]):
            raise ValueError("Call route contains an unsupported node")
        owner_id = int(owner_user_id)
        scope = ""
        if target_type == "number":
            scope = next((str(row["number"]) for row in self.list_numbers(owner_id) if str(row["number"]) == str(target)), "")
        if scope:
            # The line's own extensions only: three digits are resolved inside the
            # current phone number, so another line's set is not reachable here.
            allowed_keys = set(self.scoped_extension_keys(scope, owner_id))
            owned_extensions = {row["key"] for row in self.list_extensions(owner_id) if row["key"] in allowed_keys}
        else:
            owned_extensions = {row["key"] for row in self.list_extensions(owner_id)}
        owned_groups = {str(row["id"]): set(row["members"]) for row in self.list_groups(owner_id)}
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
            elif node_type == "ivr":
                # A menu asks for an extension that exists, and where it lands
                # when nobody enters one must be this customer's too.
                if len(str(node.get("prompt") or "")) > 400:
                    raise ValueError("IVR prompt must be 400 characters or fewer")
                if str(node.get("voice") or "platform") not in {row["id"] for row in self.IVR_VOICES}:
                    raise ValueError("Unknown IVR voice")
                if not 2 <= int(node.get("input_timeout", 0)) <= 30:
                    raise ValueError("IVR input timeout must be between 2 and 30 seconds")
                if not 1 <= int(node.get("attempts", 0)) <= 5:
                    raise ValueError("IVR attempts must be between 1 and 5")
                fallback = str(node.get("fallback") or "")
                if fallback and fallback not in owned_extensions:
                    raise ValueError("IVR fallback must be an extension owned by this customer")
            elif node_type == "business_hours":
                if not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", str(node.get("start", ""))) or not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", str(node.get("end", ""))):
                    raise ValueError("Business hours must include valid opening and closing times")
                days = node.get("days")
                if not isinstance(days, list) or not days or any(int(day) not in range(1, 8) for day in days):
                    raise ValueError("Business hours must include valid weekdays")

    def _resolve_flow_key(self, value: Any, owner_user_id: int, number: str = "") -> str:
        """The extension key a dial-plan entry means.

        The customer types `104`; the plan stores the 104 that belongs to the
        line the flow is on - `104@+13025550001` - which is what makes the entry
        reach the right desk even when another of the account's lines has a 104
        of its own. A value that is already a key is kept as written.
        """
        value = str(value or "").strip()
        if not value or extension_scope(value):
            return value
        if not value.isdigit():
            return value
        if number:
            return self.extension_key_for(number, value, owner_user_id) or value
        key = self.extension_key_for_digits(value, owner_user_id)
        if key:
            return key
        matches = [row for row in self.list_extensions(owner_user_id) if row["digits"] == value]
        if len(matches) > 1:
            raise ValueError(
                f"Extension {value} is on more than one number: pick the extension on the number this flow belongs to"
            )
        return value

    def _resolve_flow_nodes(self, route, owner_user_id: int, number: str = "") -> None:
        """Store every destination of a call flow as the extension key it names."""
        nodes = (route or {}).get("nodes") if isinstance(route, dict) else None
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            if isinstance(node.get("extensions"), list):
                node["extensions"] = [
                    self._resolve_flow_key(ext, owner_user_id, number) if str(ext or "").strip() else ext
                    for ext in node["extensions"]
                ]
            for field in ("extension", "mailbox", "fallback"):
                if str(node.get(field) or "").strip():
                    node[field] = self._resolve_flow_key(node[field], owner_user_id, number)

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
        owned = {row["key"] for row in self.list_extensions(owner_user_id)}
        members = [str(ext).strip() for ext in (data.get("members") or [])]
        members = [self._resolve_flow_key(ext, owner_user_id) for ext in members]
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
            # The legacy path: no designed flow, so the number rings the
            # extension it points at. The link holds the dialled digits, which
            # are read against this very number, so 101 means this line's 101.
            key = self.extension_key_for(number, extension, int(owner) if owner is not None else None) if extension else ""
            mailbox = key if key and any(
                item["key"] == key and item["voicemail_enabled"] for item in self.list_extensions(int(owner) if owner else None)
            ) else ""
            return {"destinations": [key] if key else [], "timeout": 30, "voicemail": mailbox, "forward": "", "outside_hours": False}
        nodes = [node for node in ((flow.get("route") or {}).get("nodes") or []) if isinstance(node, dict)]
        # A flow that carries a menu asks the caller for an extension instead of
        # ringing anybody; whatever the flow says besides that step is where an
        # unanswered or invalid entry goes.
        ivr_node = next((node for node in nodes if str(node.get("type")) == "ivr"), None)
        if ivr_node is not None:
            answer = self._ivr_answer_plan(ivr_node, owner, extension, nodes, number)
            if answer is not None:
                return answer
        return self._plan_nodes(nodes, owner, extension, number)

    def _ivr_answer_plan(self, node: dict, owner, extension: str, nodes: list[dict], number: str = "") -> dict | None:
        """What happens when the caller is asked to enter an extension.

        The menu itself - prompt, voice, how long to wait, how many times to ask
        - plus everything else the flow says, kept as the fallback plan so a
        caller who enters nothing still lands where the customer designed.
        """
        if owner is None:
            return None
        voice = self.ivr_voice(str(node.get("voice") or "platform"))
        active = {row["key"] for row in self.list_extensions(int(owner)) if row["active"]}
        if number:
            # A menu on one line asks for that line's extensions, so "104" is
            # this number's 104 rather than a device on another number.
            active &= set(self.scoped_extension_keys(number, int(owner)))
        allowed = active
        fallback = str(node.get("fallback") or "")
        tail = self._plan_nodes([item for item in nodes if item is not node], owner, extension, number)
        return {
            "kind": "ivr",
            "destinations": [],
            "extensions": sorted(allowed),
            "prompt": str(node.get("prompt") or self.IVR_DEFAULT_PROMPT),
            "voice": voice["id"], "voice_label": voice["label"], "media": f"sound:{voice['media']}",
            "input_timeout": int(node.get("input_timeout") or 6),
            "attempts": int(node.get("attempts") or 2),
            "fallback": fallback if fallback in allowed else "",
            "fallback_destinations": list(tail.get("destinations") or []),
            "voicemail": tail.get("voicemail") or "",
            "timeout": int(tail.get("timeout") or 30),
            "forward": tail.get("forward") or "",
            "outside_hours": bool(tail.get("outside_hours")),
        }

    def _plan_nodes(self, nodes: list[dict], owner, extension: str, number: str = "") -> dict:
        """The planner's body: turn a list of steps into what the engine dials."""
        allowed = {
            item["key"] for item in self.list_extensions(int(owner))
            if item["active"]
        } if owner is not None else set()
        if owner is not None and number:
            allowed &= set(self.scoped_extension_keys(number, int(owner)))
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
        first = extension_digits(members[0])
        label = f"Ring extension {first} for 25s" if len(members) == 1 else f"Ring {len(members)} devices for 25s"
        nodes = [{
            "type": "ring_group", "extensions": members, "timeout": 25,
            "label": label, "configured": True,
        }]
        if voicemail:
            nodes.append({"type": "voicemail", "mailbox": str(voicemail), "label": f"Voicemail {extension_digits(voicemail)}", "configured": True})
        return {"nodes": nodes}

    @staticmethod
    def default_extension_route(extension: str, voicemail: bool = False) -> dict:
        """The flow a new extension starts with: ring that extension, and if nobody
        picks up the call ends (voicemail is added only when it is switched on).

        The extension is named by its key, so the flow keeps meaning the right
        device even when another line has an extension with the same digits.
        """
        extension = str(extension)
        nodes = [{"type": "extension", "extension": extension, "label": f"Ring extension {extension_digits(extension)}", "configured": True}]
        if voicemail:
            nodes.append({"type": "voicemail", "mailbox": extension, "label": f"Voicemail {extension_digits(extension)}", "configured": True})
        return {"nodes": nodes}

    def extension_voicemail_enabled(self, extension: str) -> bool:
        row = self._extension_row(extension)
        return bool(row and row.get("voicemail_enabled"))

    def sync_auto_ivr(self, owner_user_id: int) -> list[str]:
        """Give every flow a menu once a customer passes five extensions.

        The operator's rule, in one place: one extension never gets a menu, past
        five every workflow gets it, and in between it stays the customer's own
        choice from the palette. Nothing is ever removed - a menu a customer
        configured is theirs.
        """
        owner_user_id = int(owner_user_id)
        active = [row for row in self.list_extensions(owner_user_id) if row["active"]]
        if not self.auto_ivr_wanted(len(active)):
            return []
        menu = self.default_ivr_node()
        added = []
        for flow in self.list_call_routes(owner_user_id):
            nodes = (flow.get("route") or {}).get("nodes") or []
            if any(str(node.get("type")) == "ivr" for node in nodes if isinstance(node, dict)):
                continue
            self.save_call_route(owner_user_id, {
                "phone_number": flow["phone_number"], "name": flow.get("name") or "Main call flow",
                "route": {"nodes": [dict(menu), *nodes]}, "active": bool(flow.get("active", True)),
            })
            added.append(f"number {flow['phone_number']}")
        for flow in self.list_routing_flows(owner_user_id):
            nodes = (flow.get("route") or {}).get("nodes") or []
            if any(str(node.get("type")) == "ivr" for node in nodes if isinstance(node, dict)):
                continue
            self.save_routing_flow(owner_user_id, {
                "name": flow.get("name") or "", "route": {"nodes": [dict(menu), *nodes]},
                "active": bool(flow.get("active", True)),
            }, target_type=flow["target_type"], target=flow["target"])
            added.append(f"{flow['target_type']} {flow['target']}")
        return added

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

        # Every number starts its own set at 101: that is the first device of the
        # line, and the number it belongs to is what makes 104 on it mean this
        # line's 104 rather than a device on another of the customer's numbers.
        extension = extension_key(self.next_extension_number(owner_user_id, number), number)
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
        # The account's main line is the one its number-less devices and its
        # profile present; provisioning a second line must not move it. Every
        # line's own extensions already call out as that line, with the caller ID
        # of the phone number the call is on.
        has_default = any(row["default_outbound"] for row in self.list_numbers(owner_user_id))
        if not has_default:
            self.set_default_outbound_number(extension, number)

        # The line rings the device it was provisioned for. A customer's main line
        # additionally picks up every device they add later (sync_primary_flows),
        # while a number tied to one extension keeps ringing only that extension.
        # Nothing else is added: no answer means the call ends, which is the
        # default the customer starts from and can extend in the builder.
        self.save_call_route(owner_user_id, {
            "phone_number": number, "name": "Main call flow" if self.primary_number(owner_user_id) in ("", number) else "Number call flow",
            # The line's own flow rings its own extensions - its 101 today, and
            # every device the customer adds to this number later (see
            # sync_primary_flows).
            "route": self.default_number_route(self.line_devices(owner_user_id, number) or [extension]),
            "active": True,
        })
        self.ensure_extension_flow(owner_user_id, extension, voicemail=self.extension_voicemail_enabled(extension))
        # A customer past five devices gets the menu on the flows this made too,
        # exactly as if they had added the device from the console.
        auto_ivr = self.sync_auto_ivr(owner_user_id)

        self.add_activity(
            owner_user_id, actor_user_id or owner_user_id, "number.provisioned", "phone_number", number,
            f"Number {number} assigned with extension {extension}, SIP credentials and default call flows",
        )
        return {
            "number": number,
            "extension": extension,
            "display_name": display_name,
            "sip_username": next(
                (str(row["sip_username"]) for row in self.list_extensions(owner_user_id) if str(row["key"]) == extension),
                username,
            ),
            "sip_password": password,
            "device_linked": bool(device),
            "default_outbound": not has_default,
            "voicemail": self.extension_voicemail_enabled(extension),
            "flows": ["number", "extension"],
            "auto_ivr": auto_ivr,
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
            # `901` names the account's 901 - there is one, or the request is
            # refused - and a key names one line's extension exactly.
            target = self._resolve_flow_key(target, owner_user_id)
            if not any(row["key"] == target for row in self.list_extensions(owner_user_id)):
                raise ValueError("Extension is not owned by this customer")
        else:
            group = self.get_group(target, owner_user_id)
            if not group:
                raise ValueError("Group not found")
            target = str(group["id"])
        route = data.get("route")
        self._resolve_flow_nodes(route, owner_user_id)
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
        # The line's own flow is written in the customer's words - 101, 104 - and
        # stored as that line's keys, so it keeps ringing this line's desks.
        self._resolve_flow_nodes(route, int(owner_user_id), phone_number)
        self._validate_route_nodes(route, int(owner_user_id), target_type="number", target=phone_number)
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
            mailbox_row = None
            if extension:
                # A profile names an extension the short way - the digits a person
                # reads - or the key once several lines carry those digits. It is
                # resolved once here, so every write below lands on one row.
                key = str(extension).strip()
                digits, scope = extension_digits(key), extension_scope(key)
                rows = db.execute(
                    "SELECT id,extension,phone_number_id FROM extensions WHERE extension=? AND active=1",
                    (digits,),
                ).fetchall()
                if scope:
                    line = db.execute("SELECT id FROM phone_numbers WHERE number=?", (scope,)).fetchone()
                    mailbox_row = next(
                        (item for item in rows if line and item["phone_number_id"] == line["id"]), None
                    )
                else:
                    plain = [item for item in rows if item["phone_number_id"] in (None, "")]
                    picked = plain or (rows if len(rows) == 1 else [])
                    mailbox_row = picked[0] if len(picked) == 1 else None
                if mailbox_row is None:
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
                if role == "user" and extension and email and mailbox_row is not None:
                    db.execute(
                        "UPDATE extensions SET voicemail_email=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (email, int(mailbox_row["id"])),
                    )
                return existing["id"]
            user_id = db.execute("INSERT INTO admin_users(username,email,extension,full_name,company_name,job_role,phone,role,active,password_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (username, email, extension, full_name, company_name, job_role, phone, role, active, password_hash)).lastrowid
            if role == "user" and extension and email and mailbox_row is not None:
                db.execute(
                    "UPDATE extensions SET voicemail_email=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (email, int(mailbox_row["id"])),
                )
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
        # The profile names one extension; the mailbox lives on it, so the row is
        # resolved exactly and a digits-only name that several rows carry changes
        # nothing rather than every one of them.
        wanted = self._extension_row(str(row["extension"])) if row and row["extension"] else None
        if wanted is not None:
            with self._connect() as db:
                db.execute(
                    "UPDATE extensions SET voicemail_email=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (email, wanted["id"]),
                )

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
            "service_web_host",
            "service_sip_port",
            "sip_auth_digest",
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
            if key in {"service_host", "service_web_host"}:
                # An address a device registers with (or the web/API domain):
                # no scheme, no path, no port - the SIP port has its own
                # setting, and a typo here breaks every phone at once.
                text = text.strip()
                if text and not self.SERVICE_HOST_RE.match(text):
                    raise ValueError("Service address must be a hostname or an IP address, without a scheme, path or port")
            if key == "sip_auth_digest" and text.strip().lower() not in {"md5", "sha256", "both"}:
                raise ValueError("SIP digest must be md5, sha256 or both")
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
                if key in {"service_host", "service_web_host"}:
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
        active = [row["key"] for row in self.list_extensions(owner_user_id) if row["active"]]

        def pick(value) -> str:
            # Stored as the extension's key; a row written before this change -
            # and an API caller reading the digits off a phone - still answers.
            value = str(value or "").strip()
            if value in active:
                return value
            if value.isdigit():
                key = self.extension_key_for_digits(value, owner_user_id)
                if key in active:
                    return key
            return active[0] if active else ""

        return {"outbound": pick(entry.get("outbound")), "fallback": pick(entry.get("fallback"))}

    def set_customer_call_defaults(self, owner_user_id: int, outbound: str, fallback: str) -> dict:
        owner_user_id = int(owner_user_id)
        owned = {row["key"] for row in self.list_extensions(owner_user_id) if row["active"]}
        resolved = {}
        for field, label, value in (
            ("outbound", "Default outbound extension", outbound),
            ("fallback", "Inbound fallback extension", fallback),
        ):
            value = str(value or "").strip()
            if value and value not in owned:
                # The three digits name the account's extension with them; two
                # lines that both hold 101 have to be named by their key.
                key = self.extension_key_for_digits(value, owner_user_id) if value.isdigit() else ""
                if key not in owned:
                    if value.isdigit() and any(row["digits"] == value for row in self.list_extensions(owner_user_id) if row["active"]):
                        raise ValueError(
                            f"{label}: {value} is on more than one of your numbers - choose the extension on the number it belongs to"
                        )
                    raise ValueError(f"{label} must be one of your active extensions")
                value = key
            resolved[field] = value
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE `key`=?", (self.CALL_DEFAULTS_KEY,)).fetchone()
        try:
            stored = json.loads(row["value"]) if row else {}
        except (TypeError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored[str(owner_user_id)] = {"outbound": resolved["outbound"], "fallback": resolved["fallback"]}
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
        if legacy:
            # A platform-wide last resort from before customers owned this
            # decision. It only counts for the platform's own line: a three-digit
            # number is resolved inside the current phone number, so it is never
            # borrowed by a customer's call.
            row = next(
                (item for item in self.list_extensions() if item["digits"] == extension_digits(legacy) and item["active"]),
                None,
            )
            if row is not None and not row["number"] and row.get("owner_user_id") in (None, ""):
                return str(row["key"])
        return ""

    def _digits_are_numberless(self, digits: Any) -> bool:
        """Do these digits name a row that has no phone number of its own?

        Those are the platform's own devices. A legacy default written as `101`
        can only mean one of them: an extension that answers inside a phone
        number is never reached by its digits alone, so a bare `101` must not
        hold a line's `101` in place while that line is being taken down.
        """
        text = extension_digits(digits)
        if not text:
            return False
        with self._connect() as db:
            # `phone_number_id IS NULL`, not a join that also reads true for a
            # pointer whose number was just deleted: that is the ambiguity this
            # guard exists to rule out, not a device without a line.
            row = db.execute(
                "SELECT 1 FROM extensions WHERE extension=? AND phone_number_id IS NULL LIMIT 1",
                (text,),
            ).fetchone()
        return row is not None

    def extension_is_a_call_default(self, extension: str) -> bool:
        """Is any customer relying on this extension as their outbound or fallback?

        What is compared is the exact key - the identity every reference uses
        now. Bare digits only count while they are the whole identity of a row
        with no number of its own (a platform device), because inside a number
        the digits alone never name a device: reading them as one is what used
        to keep a line's `101` alive after the number it answered on was gone.
        """
        extension = str(extension)
        digits = extension_digits(extension)
        # A bare `101` names a device only where nothing else can: on a line the
        # same digits are a different device on every line.
        digits_name_a_numberless_row = self._digits_are_numberless(digits)

        def is_it(value) -> bool:
            text = str(value or "").strip()
            if not text:
                return False
            if text == extension:
                return True
            return digits_name_a_numberless_row and text.isdigit() and text == digits

        try:
            stored = json.loads(self.get_settings().get(self.CALL_DEFAULTS_KEY) or "{}")
        except (TypeError, ValueError):
            stored = {}
        if any(is_it(entry.get(field)) for entry in stored.values() if isinstance(entry, dict) for field in ("outbound", "fallback")):
            return True
        with self._connect() as db:
            rows = db.execute(
                "SELECT value FROM settings WHERE `key` IN ('default_extension','inbound_fallback_extension')"
            ).fetchall()
        return any(is_it(row["value"]) for row in rows)

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
            # The console speaks the digits a customer reads; the row is stored
            # under its key. A bare `901` still names the row that has no number of
            # its own.
            wanted = str(target)
            owner = next((row["owner_user_id"] for row in store.list_extensions() if str(row["key"]) == wanted), None)
            if owner is None and wanted.isdigit():
                # The digits name one extension while one of the account's rows
                # carries them; where several do, the caller has to name the
                # phone number as well.
                key = store.extension_key_for_digits(wanted)
                owner = next((row["owner_user_id"] for row in store.list_extensions() if str(row["key"]) == key), None)
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

    @app.get("/console-check")
    @login_required
    def console_check_page():
        """A layout self-check that runs in the browser that uses the console.

        The automated harnesses run in jsdom, which has no layout engine, so the
        geometry of the customer workspace drawer - what stays still, what
        scrolls, what pins - can only be measured where it is really rendered.
        The page reads positions and sizes and shows nothing about any customer.
        """
        return send_from_directory(web_dir, "console-check.html")

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
        owned_extensions = [row["key"] for row in extensions]
        if is_admin:
            voicemail_messages = current_app.extensions["voicemail_store"].list_messages()
            visible_numbers = store.list_numbers()
        else:
            voicemail_messages = [
                message for ext in owned_extensions
                for message in current_app.extensions["voicemail_store"].list_messages(extension_mailbox(ext))
            ]
            visible_numbers = store.list_numbers(user_id)
            for number in visible_numbers:
                number.pop("provider", None)
        all_users = store.list_users() if is_admin else []
        all_sip = store.list_sip_accounts(None if is_admin else user_id)
        # Whether a phone is signed in right now. A phone that is not registered
        # is the first thing to check when a call does not go through, so every
        # extension and device carries it.
        endpoint_states = current_app.extensions["telephony_service"].endpoint_states()
        if endpoint_states:
            for row in all_sip:
                row["registration_status"] = device_registration(row, endpoint_states)
            for row in extensions:
                row["registration_status"] = extension_registration(row, endpoint_states, all_sip)
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
            "call_summary": current_app.extensions["telephony_service"].store.summary(
                extensions=store.call_extension_names(user_id) if not is_admin else None,
            ),
            "voicemail_summary": {
                "total": len(voicemail_messages), "new": sum(row["folder"] == "inbox" for row in voicemail_messages),
                "old": sum(row["folder"] == "old" for row in voicemail_messages), "urgent": sum(row["folder"] == "urgent" for row in voicemail_messages),
            },
            "settings": store.get_settings() if is_admin else {},
            # The device provisioning generated with the customer's first
            # number. An administrator owns none, so it is empty for them.
            "primary_extension": store.primary_extension(user_id) if not is_admin else "",
            # The menu a caller can be given: the voices this deployment can
            # play, the default text, and the point at which the platform adds it
            # to a customer's flows by itself.
            "ivr_voices": store.ivr_voices(),
            "ivr_default_prompt": store.IVR_DEFAULT_PROMPT,
            "ivr_auto_above": store.IVR_AUTO_ABOVE,
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
            "extensions": [row["key"] for row in store.list_extensions(customer_id) if row["active"]],
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
            requested = str(data.get("extension", "")).strip()
            # `101` is not an identity any more: it names the line's own 101 when
            # one row carries those digits, so an edit lands on that row instead of
            # inventing a second extension beside it. With a number named, the
            # request already says which line it means.
            scope = str(data.get("number") or "").strip() or extension_scope(requested)
            if requested.isdigit() and not scope:
                # Two of the account's lines may both hold 101: guessing one would
                # put the device on the wrong desk, so the request has to say which
                # number it means.
                if owner_id is not None:
                    holding = [row for row in store.list_extensions(owner_id) if row["digits"] == requested]
                    if len(holding) > 1 and not any(not row["number"] for row in holding):
                        raise ValueError(
                            f"Extension {requested} is on more than one number: choose the number this extension belongs to"
                        )
                existing_key = store.extension_key_for_digits(requested, owner_id)
                if existing_key:
                    data = {**data, "extension": existing_key}
                    requested = existing_key
            scope = str(data.get("number") or "").strip() or extension_scope(requested)
            canonical = extension_key(extension_digits(requested), scope) if extension_digits(requested) else requested
            previous_owner = store.get_extension_owner(canonical) if canonical else None
            existed = any(row["key"] == canonical for row in store.list_extensions())
            result = store.save_extension(data, owner_id, session.get("admin_role") != "admin")
            # A moved extension changes who answers a number every phone already
            # dials, so it is recorded for the operator and the account that lost
            # it is told - their phones can no longer reach it.
            new_owner = store.get_extension_owner(result)
            if previous_owner is not None and not same_account(previous_owner, new_owner):
                store.add_activity(
                    new_owner or int(session["admin_user_id"]), int(session["admin_user_id"]),
                    "extension.reassigned", "extension", result,
                    f"Extension {result} moved from {store._account_label(previous_owner)} "
                    f"to {store._account_label(new_owner)}",
                )
                store.add_notification(
                    int(previous_owner), "extension", "Extension reassigned",
                    f"Extension {result} was moved to another account and is no longer in your dial plan.",
                )
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
                    # Past the fifth device a customer needs a menu more than a
                    # list of extensions; the rule lives in the store.
                    store.sync_auto_ivr(owner_id)
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
            key, problem = store.resolve_requested_extension(extension, owner)
            if problem:
                return jsonify({"error": problem}), 400
            extension = key
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
            key, problem = store.resolve_requested_extension(extension, owner)
            if problem:
                return jsonify({"error": problem}), 404
            return jsonify({"credentials": store.reveal_extension_credentials(
                key, owner, request.host.split(":")[0] if request.host else "",
            )})
        except ValueError as exc: return jsonify({"error": str(exc)}), 404

    @app.get("/admin/api/extensions/next")
    @login_required
    def admin_next_extension():
        """The extension number a new line would get, for the console to prefill.

        Naming the line answers for that line: every number starts its own set at
        101, so the number the device will live on decides what comes next.
        """
        number = str(request.args.get("number") or "").strip()
        owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
        if number and owner is None:
            owner = store.get_number_owner(number)
        elif number and owner is not None and store.get_number_owner(number) not in (None, owner):
            # Another customer's line: its free numbers are not this caller's to see.
            return jsonify({"error": "Phone number not found"}), 404
        return jsonify({
            "extension": store.next_extension_number(owner, number),
            "number": number,
        })

    @app.delete("/admin/api/extensions/<extension>")
    @login_required
    def admin_delete_extension(extension):
        try:
            owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
            key, problem = store.resolve_requested_extension(extension, owner)
            if problem:
                return jsonify({"error": problem}), 400
            extension = key
            row = store._extension_row(extension)
            mailboxes = [extension]
            if row:
                mailboxes = [str(row["key"]), str(row["mailbox"]), str(row["digits"])]
            if any(current_app.extensions["voicemail_store"].list_messages(box) for box in dict.fromkeys(mailboxes)):
                raise ValueError("Cannot delete an extension that still has voicemail messages")
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
                # Auto-provisioning builds the line for a customer; without one
                # it would silently store an unlinked number that rings nobody.
                if not owner:
                    raise ValueError("Auto-provisioning needs a customer account: choose who owns this number, or pick an extension explicitly")
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

    @app.get("/admin/api/numbers/<path:number>/extensions")
    @login_required
    def admin_number_extensions(number):
        """Every extension that belongs to one phone number, in dialling order.

        The list is the number's own set and nothing else: another line of the
        same customer has its own 101, and this is what the console draws beside
        one number.
        """
        is_admin = session.get("admin_role") == "admin"
        owner = None if is_admin else int(session["admin_user_id"])
        line = store.extension_number_row(number)
        if line is None:
            return jsonify({"error": "Phone number not found"}), 404
        if not is_admin and line["owner_user_id"] not in (None, owner):
            return jsonify({"error": "Phone number not found"}), 404
        number_owner = line["owner_user_id"] if is_admin else owner
        rows = store.extensions_on_number(number_owner, number)
        live = current_app.extensions["telephony_service"].endpoint_states()
        accounts = store.list_sip_accounts(number_owner)
        for row in rows:
            if live:
                row["registration_status"] = extension_registration(row, live, accounts)
        return jsonify({
            "number": number,
            "inbound_extension": str(line["inbound_extension"] or ""),
            "owner_user_id": line["owner_user_id"],
            "extensions": rows,
        })

    @app.post("/admin/api/numbers/<path:number>/extensions")
    @login_required
    def admin_add_number_extension(number):
        """Add the next extension to one phone number.

        Every number's set starts at 101 and goes on from there, so this is the
        console's "Add extension" on a line: the next free number on this very
        number, with credentials and a default flow.
        """
        try:
            data = request.get_json(silent=True) or {}
            is_admin = session.get("admin_role") == "admin"
            owner = int(session["admin_user_id"]) if not is_admin else None
            if is_admin and str(data.get("owner_user_id", "")).isdigit():
                owner = int(data["owner_user_id"])
            if owner is None:
                owner = store.get_number_owner(number)
            if not is_admin and owner is not None and owner != int(session["admin_user_id"]):
                raise ValueError("Number is not assigned to your account")
            result = store.add_extension_to_number(number, owner, data)
            store.sync_primary_flows(owner, result["extension"])
            store.sync_auto_ivr(owner)
            store.add_activity(
                owner, int(session["admin_user_id"]), "extension.provisioned", "extension", result["extension"],
                f"Extension {result['digits']} added to {number} with SIP credentials and a default call flow",
            )
            apply_change()
            return jsonify({
                "ok": True, "extension": result["key"], "key": result["key"], "digits": result["digits"],
                "number": result["number"], "created": True,
                "credentials": store.reveal_extension_credentials(result["key"], owner),
            })
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400

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
            owner = int(session["admin_user_id"])
            # The history reads the exact extension (`101@+13025550001`), while a
            # caller may name the digits - which name one row of this account, and
            # nothing at all when several carry them.
            owned_extensions = store.call_extension_names(owner)
            if extension.isdigit():
                key, problem = store.resolve_requested_extension(extension, owner)
                if problem:
                    return jsonify({"error": problem}), (404 if problem == "Extension not found" else 400)
                extension = key
            if extension and extension not in owned_extensions:
                return jsonify({"error": "extension not found"}), 404
        status = request.args.get("status", "").strip()
        query = request.args.get("q", "").strip()[:100]
        number = request.args.get("number", "").strip()
        recordings_only = request.args.get("recordings", "false").lower() == "true"
        if extension and not (
            (extension.isdigit() and 100 <= int(extension) <= 999) or extension_scope(extension)
        ):
            return jsonify({"error": "invalid extension"}), 400
        if number:
            if not re.fullmatch(r"\+[1-9]\d{7,14}", number):
                return jsonify({"error": "invalid number"}), 400
            visible_numbers = store.list_numbers(None if session.get("admin_role") == "admin" else int(session["admin_user_id"]))
            if not any(str(row["number"]) == number for row in visible_numbers):
                return jsonify({"error": "number not found"}), 404
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
            query=query or None, number=number or None, limit=limit, offset=offset,
        )
        return jsonify({"calls": [call.to_dict() for call in calls], "total": total, "limit": limit, "offset": offset})

    def visible_call_rows():
        calls = current_app.extensions["telephony_service"].store.all()
        if session.get("admin_role") == "admin":
            return calls
        owned = set(store.call_extension_names(int(session["admin_user_id"])))
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
        """May this session hear that mailbox?

        A mailbox is a key (`101@+13025550001`), its name on disk
        (`101-13025550001`) or the digits a customer reads (`101`), so all three
        are answered - and only for the account that owns the extension.
        """
        if session.get("admin_role") == "admin":
            return True
        wanted = str(mailbox)
        for row in store.list_extensions(int(session["admin_user_id"])):
            key = str(row["key"])
            if wanted in {key, str(row["mailbox"]), extension_digits(key)}:
                return True
        return False

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
        owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
        available = store.list_extensions(owner)
        if extension.isdigit():
            # The app and the API send the three digits their user dialled. They
            # name one extension while only one of the account's carries them;
            # with several the phone number has to be named as well.
            if len([row for row in available if row["digits"] == extension]) > 1:
                return jsonify({
                    "error": f"{extension} is on more than one of your numbers - name the phone number as well"
                }), 400
            extension = store.extension_key_for_digits(extension, owner)
        if not any(row["key"] == extension and row["active"] for row in available):
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
        owner = None if session.get("admin_role") == "admin" else int(session["admin_user_id"])
        # `101` may name a line's own extension: resolve it to the key first, so
        # the caller ID is set on the device the console was looking at.
        if extension.isdigit():
            if len([row for row in store.list_extensions(owner) if row["digits"] == extension]) > 1:
                return jsonify({
                    "error": f"{extension} is on more than one of your numbers - name the phone number as well"
                }), 400
            extension = store.extension_key_for_digits(extension, owner)
        if owner is not None and not any(row["key"] == extension for row in store.list_extensions(owner)):
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
            owner = int(session["admin_user_id"])
            owned = [row["key"] for row in store.list_extensions(owner)]
            # The customer picks an extension; its messages live in that
            # extension's own mailbox (`101-13025550001`).
            if mailbox and mailbox.isdigit():
                # Two of the account's lines may both hold 101, and their messages
                # are two mailboxes: guessing one would show the wrong line's
                # voicemail, so the request has to name the mailbox.
                holding = [row for row in store.list_extensions(owner) if row["digits"] == mailbox]
                if len(holding) > 1:
                    return jsonify({
                        "error": f"{mailbox} is on more than one of your numbers - choose the mailbox "
                                 "on the number it belongs to",
                    }), 400
                if holding:
                    mailbox = extension_mailbox(str(holding[0]["key"]))
            if mailbox and mailbox not in owned and mailbox_name(mailbox) not in {
                extension_mailbox(str(ext)) for ext in owned
            }:
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
        if call and session.get("admin_role") != "admin" and not any(
            row["key"] == call.extension for row in store.list_extensions(int(session["admin_user_id"]))
        ):
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
                "service_host", "service_web_host", "service_sip_port", "sip_auth_digest",
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
        calls, total = current_app.extensions["telephony_service"].store.search(extensions=[row["key"] for row in extensions], limit=20)
        return jsonify({
            "customer": next((row for row in store.list_users() if row["id"] == customer_id), customer),
            "extensions": extensions, "numbers": numbers, "sip_accounts": store.list_sip_accounts(customer_id),
            # Which of those extensions the platform generated first: the
            # workspace marks it the customer's primary device.
            "primary_extension": store.primary_extension(customer_id),
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
        owner = None if is_admin else int(session["admin_user_id"])
        accounts = store.list_sip_accounts(owner)
        live = current_app.extensions["telephony_service"].endpoint_states()
        extensions = store.list_extensions(owner)
        return jsonify({
            "devices": [
                {"id": row["id"], "registration_status": device_registration(row, live) if live else row["registration_status"]}
                for row in accounts
            ],
            # The extensions a customer actually signs in with - most phones
            # register as the extension itself rather than as a device account.
            "extensions": [
                {"extension": row["key"], "registration_status": extension_registration(row, live, accounts)}
                for row in extensions if live
            ],
        })

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
            # A flow saved by a customer who is past five devices still gets the
            # menu: the rule is about every workflow, not only the ones that
            # existed when the sixth device was created. What was added is
            # reported, so the console can say it instead of changing the stored
            # flow silently.
            added = store.sync_auto_ivr(owner)
            who = "operator" if session.get("admin_role") == "admin" else "customer"
            kind = "call_route" if target_type == "number" else "routing_flow"
            store.add_activity(owner, int(session["admin_user_id"]), "route.saved", kind, route_id, f"Call flow for {label} updated by the {who}")
            return jsonify({"ok": True, "route_id": route_id, "auto_ivr": added})
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
            owner = int(session["admin_user_id"])
            owned = store.list_extensions(owner)
            if not extension and len(owned) == 1:
                extension = owned[0]["key"]
            if not extension and len(owned) > 1:
                return jsonify({
                    "error": "Name the extension whose recording you want to change - "
                             "this account has more than one"
                }), 400
            if extension.isdigit():
                if len([row for row in owned if row["digits"] == extension]) > 1:
                    return jsonify({
                        "error": f"{extension} is on more than one of your numbers - name the phone number as well"
                    }), 400
                extension = store.extension_key_for_digits(extension, owner)
            if not any(row["key"] == extension for row in owned):
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
