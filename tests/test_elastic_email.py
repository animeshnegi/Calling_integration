"""Elastic Email-only v2 contract, automatic legacy migration and image-only DB upgrades."""
import base64
import hashlib
import io
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests
from cryptography.fernet import Fernet
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from app.admin import SettingsStore
from app.database import metadata, reconcile_statements
from app.voicemail import VoicemailStore
from app.voicemail_email import ELASTIC_EMAIL_URL, VoicemailEmailNotifier


@pytest.fixture
def elastic_system(tmp_path):
    settings = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    settings.save_email_config({"provider": "elasticemail", "api_key": "ELASTIC-secret", "enabled": True,
                                "from_email": "verified@example.com", "from_name": "Voicemail Team"})
    settings.save_extension({"extension": "101", "sip_password": "sip-secret", "voicemail_enabled": True,
                             "voicemail_pin": "1234", "voicemail_email": "agent@example.com"})
    store = VoicemailStore(str(tmp_path / "voicemail"))
    return settings, store, VoicemailEmailNotifier(store, settings)


def test_real_form_posts_match_v2_and_voicemail_uploads_audio(elastic_system, monkeypatch):
    """Exercise requests' actual wire encoding against a local fake provider.

    A plain test uses application/x-www-form-urlencoded, exactly as the user's
    example; a voicemail uses multipart, with the original bytes as a file part.
    No Elastic Email credential or network service is needed by this test.
    """
    assert ELASTIC_EMAIL_URL == "https://api.elasticemail.com/v2/email/send"
    settings, store, notifier = elastic_system
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            builder = EnvironBuilder(method="POST", path=self.path, input_stream=io.BytesIO(raw),
                                     content_type=self.headers["Content-Type"], content_length=len(raw))
            request = Request(builder.get_environ())
            files = [(key, part.filename, part.content_type, part.read())
                     for key in request.files for part in request.files.getlist(key)]
            captured.append({"path": self.path, "content_type": self.headers["Content-Type"],
                             "authorization": self.headers.get("Authorization"),
                             "form": request.form.to_dict(), "files": files})
            body = json.dumps({"success": True, "data": {"transactionid": "test", "messageid": "test"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            builder.close()

        def log_message(self, *args):
            pass  # Don't log credential-bearing request data.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr("app.voicemail_email.ELASTIC_EMAIL_URL", f"http://127.0.0.1:{server.server_port}/v2/email/send")
        assert notifier.send_test("test@example.com") == (True, None)
        settings.save_email_config({"transcription_enabled": True})
        monkeypatch.setattr("app.voicemail_email.local_transcription.transcribe",
                            lambda audio, suffix: ("Please <call> me.", "transcribed"))
        directory = store.root / store.context / "101" / "INBOX"
        directory.mkdir(parents=True)
        (directory / "msg0000.txt").write_text("[message]\ncallerid=Customer <script>\norigtime=1700000000\nduration=15\n")
        audio = directory / "msg0000.wav"
        audio.write_bytes(b"RIFF\x00\x01voicemail-test")
        os.utime(audio, (time.time() - 20, time.time() - 20))
        assert notifier.process() == 1
        assert notifier.process() == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert len(captured) == 2
    test, voicemail = captured
    assert test["path"] == voicemail["path"] == "/v2/email/send"  # No API key in URL.
    assert test["authorization"] is voicemail["authorization"] is None
    assert test["content_type"].startswith("application/x-www-form-urlencoded")
    assert test["files"] == []
    assert test["form"] == {
        "apikey": "ELASTIC-secret", "subject": "EngineerIP email delivery test",
        "from": "verified@example.com", "fromName": "Voicemail Team", "to": "test@example.com",
        "bodyText": "Your EngineerIP email configuration is working. This test contains no voicemail audio and does not test transcription.\n\nThis is an automated message. The sending email address is not associated with a user. Please do not reply.",
        "bodyHtml": "<p>Your EngineerIP email configuration is working.</p><p>This test contains no voicemail audio and does not test transcription.</p><p>This is an automated message. The sending email address is not associated with a user. Please do not reply.</p>",
        "isTransactional": "true",
    }
    assert voicemail["content_type"].startswith("multipart/form-data;")
    assert voicemail["form"]["apikey"] == "ELASTIC-secret"
    assert voicemail["form"]["from"] == "verified@example.com"
    assert voicemail["form"]["to"] == "agent@example.com"
    assert voicemail["form"]["isTransactional"] == "true"
    assert "Please <call> me." in voicemail["form"]["bodyText"]
    assert "Please &lt;call&gt; me." in voicemail["form"]["bodyHtml"]
    assert "<script>" not in voicemail["form"]["bodyHtml"]
    assert voicemail["files"] == [("file", "voicemail-101-msg0000.wav", "audio/wav", b"RIFF\x00\x01voicemail-test")]
    delivery = settings.list_voicemail_deliveries()[0]
    assert delivery["status"] == "delivered" and delivery["transcription_status"] == "transcribed"
    assert "ELASTIC-secret" not in str(delivery) and "Please <call>" not in str(delivery)


def fake_response(status, payload):
    def body():
        if isinstance(payload, Exception):
            raise payload
        return payload
    return SimpleNamespace(status_code=status, json=body, text="ELASTIC-secret <script> untrusted")


@pytest.mark.parametrize("status,payload,expected", [
    (200, {"success": False, "error": "Invalid API key ELASTIC-secret <script>"}, "Send HTTP"),
    (400, {"success": False, "error": "Sender domain is not verified ELASTIC-secret"}, "Verify the sender"),
    (401, {"success": False, "error": "ELASTIC-secret"}, "Send HTTP"),
    (403, {"success": False, "error": "Permission denied ELASTIC-secret"}, "Send HTTP"),
    (413, {"success": False, "error": "ELASTIC-secret"}, "too large"),
    (429, {"success": False, "error": "ELASTIC-secret"}, "Rate limit"),
    (503, {"success": True, "error": "ELASTIC-secret"}, "unavailable"),
    (302, {"success": True}, "Elastic Email HTTP 302"),
    (200, {}, "acceptance is unknown"),
    (200, {"success": "true"}, "acceptance is unknown"),
    (200, ValueError("ELASTIC-secret"), "acceptance is unknown"),
])
def test_elastic_rejections_never_report_success_or_leak_response(elastic_system, monkeypatch, status, payload, expected):
    _, _, notifier = elastic_system
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda *a, **kw: calls.append((a, kw)) or fake_response(status, payload))
    ok, error = notifier.send_test("test@example.com")
    assert not ok and expected in error
    assert "ELASTIC-secret" not in error and "<script>" not in error and "test@example.com" not in error
    assert len(calls) == 1 and calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("error,expected", [(requests.Timeout("ELASTIC-secret"), "timed out"),
                                           (requests.ConnectionError("ELASTIC-secret"), "Cannot reach")])
def test_elastic_network_failures_are_safe(elastic_system, monkeypatch, error, expected):
    _, _, notifier = elastic_system
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr("app.voicemail_email.requests.post", fail)
    ok, message = notifier.send_test("test@example.com")
    assert not ok and expected in message and "Elastic Email" in message
    assert "ELASTIC-secret" not in message


@pytest.mark.parametrize("legacy_provider", [None, "sendgrid", "elasticemail"])
def test_old_saved_elastic_key_automatically_sends_via_elastic(tmp_path, monkeypatch, legacy_provider):
    """Old provider flags and old browsers cannot route a saved Elastic key to SendGrid."""
    path = tmp_path / "old.db"
    secret = "s" * 40
    encryption_key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    ciphertext = Fernet(encryption_key).encrypt(b"ELASTIC-legacy-key").decode()
    with sqlite3.connect(path) as db:
        provider_col = ", provider TEXT NOT NULL DEFAULT 'sendgrid'" if legacy_provider is not None else ""
        db.execute(f"""CREATE TABLE email_config(id INTEGER PRIMARY KEY,sendgrid_api_key_enc TEXT NOT NULL,
                     from_email TEXT NOT NULL,from_name TEXT NOT NULL,enabled INTEGER NOT NULL,
                     updated_at TEXT DEFAULT CURRENT_TIMESTAMP{provider_col})""")
        db.execute("INSERT INTO email_config(id,sendgrid_api_key_enc,from_email,from_name,enabled) VALUES(1,?,?,?,1)",
                   (ciphertext, "verified@example.com", "Old sender"))
        if legacy_provider is not None:
            db.execute("UPDATE email_config SET provider=?", (legacy_provider,))
        db.execute("""CREATE TABLE voicemail_deliveries(fingerprint TEXT PRIMARY KEY, mailbox TEXT NOT NULL,
                   recipient TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL,
                   last_error TEXT,delivered_at TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("INSERT INTO voicemail_deliveries(fingerprint,mailbox,recipient,status,attempts,last_error) VALUES('failed','101','agent@example.com','failed',5,'SendGrid HTTP 401')")
        db.execute("INSERT INTO voicemail_deliveries(fingerprint,mailbox,recipient,status,attempts) VALUES('done','101','agent@example.com','delivered',1)")
        db.execute("INSERT INTO voicemail_deliveries(fingerprint,mailbox,recipient,status,attempts,last_error) VALUES('uncertain','101','agent@example.com','failed',5,'SendGrid request timed out; acceptance is unknown')")
        db.execute("INSERT INTO voicemail_deliveries(fingerprint,mailbox,recipient,status,attempts,last_error) VALUES('other','101','agent@example.com','failed',5,'Elastic Email HTTP 401')")
    settings = SettingsStore(str(path), secret)
    existing = settings.get_email_config(include_key=True)
    assert existing["provider"] == "elasticemail"
    assert existing["api_key"] == "ELASTIC-legacy-key" and existing["enabled"]
    with settings._connect() as db:
        row = db.execute("SELECT provider,sendgrid_api_key_enc FROM email_config").fetchone()
        assert row["provider"] == "elasticemail" and row["sendgrid_api_key_enc"] == ciphertext
        attempts = {r["fingerprint"]: (r["status"], r["attempts"], r["last_error"]) for r in db.execute("SELECT fingerprint,status,attempts,last_error FROM voicemail_deliveries").fetchall()}
    assert attempts == {"failed": ("failed", 0, None), "done": ("delivered", 1, None),
                        "uncertain": ("failed", 5, "SendGrid request timed out; acceptance is unknown"),
                        "other": ("failed", 5, "Elastic Email HTTP 401")}
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda url, **kw: calls.append((url, kw)) or fake_response(200, {"success": True}))
    notifier = VoicemailEmailNotifier(VoicemailStore(str(tmp_path / "voicemail")), settings)
    assert notifier.send_test("test@example.com") == (True, None)
    assert calls[0][0] == ELASTIC_EMAIL_URL and calls[0][1]["data"]["apikey"] == "ELASTIC-legacy-key"
    # A still-open old page posting SendGrid with a blank key must not switch
    # providers, drop the key, or require a new key to be pasted.
    settings.save_email_config({"provider": "sendgrid", "api_key": "", "from_name": "New sender"})
    assert settings.get_email_config(include_key=True)["api_key"] == "ELASTIC-legacy-key"
    assert settings.get_email_config()["provider"] == "elasticemail"
    assert notifier.send_test("test@example.com") == (True, None)
    # Automatic voicemail sends use the same migrated key and endpoint.
    settings.save_extension({"extension": "101", "sip_password": "sip-secret", "voicemail_enabled": True,
                             "voicemail_pin": "1234", "voicemail_email": "agent@example.com"})
    directory = tmp_path / "voicemail" / "engineerip" / "101" / "INBOX"
    directory.mkdir(parents=True)
    (directory / "msg0000.txt").write_text("[message]\ncallerid=Customer\norigtime=1700000000\nduration=4\n")
    audio = directory / "msg0000.wav"
    audio.write_bytes(b"RIFF-legacy")
    os.utime(audio, (time.time() - 20, time.time() - 20))
    assert notifier.process() == 1 and notifier.process() == 0
    assert calls[-1][1]["files"][0][1][1] == b"RIFF-legacy"
    assert all(call[0] == ELASTIC_EMAIL_URL for call in calls)
    assert "api_key" not in settings.get_email_config()
    with settings._connect() as db:
        assert "ELASTIC-legacy-key" not in str(dict(db.execute("SELECT * FROM email_config").fetchone()))
        db.execute("UPDATE voicemail_deliveries SET attempts=5 WHERE fingerprint='failed'")
    # Restarting both workers must not keep resetting already retried failures.
    again = SettingsStore(str(path), secret)
    assert next(row for row in again.list_voicemail_deliveries() if row["fingerprint"] == "failed")["attempts"] == 5


def test_recognisable_sendgrid_key_is_not_forwarded_to_elastic(tmp_path, monkeypatch):
    settings = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    settings.save_email_config({"api_key": "ELASTIC-secret", "from_email": "verified@example.com"})
    # A real old SendGrid key must not be sent to a different company.
    with settings._connect() as db:
        db.execute("UPDATE email_config SET provider='sendgrid',sendgrid_api_key_enc=?", (settings.encrypt("SG.old-secret"),))
    settings = SettingsStore(str(settings.path), "s" * 40)
    notifier = VoicemailEmailNotifier(VoicemailStore(str(tmp_path / "voicemail")), settings)
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda *args, **kw: calls.append(args) or fake_response(200, {"success": True}))
    ok, error = notifier.send_test("test@example.com")
    assert not ok and "Elastic Email API key" in error and "SG.old-secret" not in error
    assert calls == []
    with pytest.raises(ValueError, match="Enter an Elastic Email API key"):
        settings.save_email_config({"api_key": "SG.other-secret"})
    settings.save_email_config({"api_key": "ELASTIC-replacement"})
    assert notifier.send_test("test@example.com") == (True, None)
    assert calls == [(ELASTIC_EMAIL_URL,)]


def test_mysql_upgrade_adds_provider_without_changing_legacy_key():
    existing = {t.name: {c.name: {"type": c.type} for c in t.columns} for t in metadata.sorted_tables}
    del existing["email_config"]["provider"]
    ddl = "\n".join(reconcile_statements(existing))
    assert "ADD COLUMN provider VARCHAR(20) NOT NULL DEFAULT 'elasticemail'" in ddl
    assert "sendgrid_api_key_enc" not in ddl


def test_failed_voicemail_is_retried_after_provider_repair_without_double_sending(elastic_system, monkeypatch):
    settings, store, notifier = elastic_system
    directory = store.root / store.context / "101" / "INBOX"
    directory.mkdir(parents=True)
    (directory / "msg0000.txt").write_text("[message]\ncallerid=Customer\norigtime=1700000000\nduration=4\n")
    audio = directory / "msg0000.gsm"
    audio.write_bytes(b"gsm-audio-test")
    os.utime(audio, (time.time() - 20, time.time() - 20))
    sent = []
    responses = [fake_response(200, {"success": False, "error": "Invalid API key ELASTIC-secret"}),
                 fake_response(200, {"success": True, "data": {"messageid": "accepted"}})]
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda url, **kw: sent.append((url, kw)) or responses.pop(0))
    assert notifier.process() == 0
    assert notifier.process() == 0  # Failed attempts have a retry delay.
    delivery = settings.list_voicemail_deliveries()[0]
    assert delivery["status"] == "failed" and "Send HTTP" in delivery["last_error"]
    assert "ELASTIC-secret" not in str(delivery)
    settings.save_email_config({"api_key": "ELASTIC-repaired"})
    with settings._connect() as db:
        db.execute("UPDATE voicemail_deliveries SET updated_at=datetime('now','-2 minutes') WHERE status='failed'")
    assert notifier.process() == 1 and notifier.process() == 0
    assert len(sent) == 2 and sent[1][1]["data"]["apikey"] == "ELASTIC-repaired"
    assert sent[1][1]["files"] == [("file", ("voicemail-101-msg0000.gsm", b"gsm-audio-test", "audio/gsm"))]
    assert settings.list_voicemail_deliveries()[0]["status"] == "delivered"


@pytest.mark.parametrize("provider", [None, "sendgrid", "elasticemail", "https://unexpected.example"])
def test_legacy_or_invalid_provider_flag_never_changes_the_sender(monkeypatch, provider):
    """The transport cannot regress to SendGrid, even for a stale DB/browser."""
    config = {"api_key": "ELASTIC-saved", "from_email": "verified@example.com", "provider": provider}
    message = {"mailbox": "test", "message": "test", "duration_seconds": 0}
    payload = VoicemailEmailNotifier._payload(config, "to@example.com", message, None)
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post",
                        lambda url, **kw: calls.append((url, kw)) or fake_response(200, {"success": True}))
    assert VoicemailEmailNotifier._send(config, payload) == (True, None)
    assert len(calls) == 1 and calls[0][0] == ELASTIC_EMAIL_URL
    assert calls[0][1]["data"]["apikey"] == "ELASTIC-saved"
    assert calls[0][1]["allow_redirects"] is False


def test_mysql_migration_queries_are_parameterized_and_only_reset_once():
    # Exercise the actual SQLite-to-PyMySQL adapter and %-formatting without a
    # running MySQL server. A literal LIKE '%' in the SQL would crash PyMySQL.
    from app.database import MySQLConnection

    class Raw:
        def __init__(self):
            self.calls = []
            self.marked = False
        def cursor(self):
            raw = self
            class Cursor:
                rowcount = 0
                lastrowid = 0
                def execute(self, sql, params):
                    formatted = sql % tuple(repr(param) for param in params)
                    raw.calls.append((sql, params, formatted))
                    if sql.startswith("INSERT IGNORE"):
                        self.rowcount = 0 if raw.marked else 1
                        raw.marked = True
            return Cursor()

    raw = Raw()
    db = MySQLConnection(raw)
    SettingsStore._migrate_email_to_elastic(db)
    SettingsStore._migrate_email_to_elastic(db)
    resets = [call for call in raw.calls if "UPDATE voicemail_deliveries" in call[0]]
    assert len(resets) == 1
    assert "LIKE %s" in resets[0][0] and resets[0][1] == ("SendGrid HTTP 4%",)
    assert "WHERE status='failed'" in resets[0][0]
    assert len([call for call in raw.calls if "UPDATE email_config" in call[0]]) == 2


def test_concurrent_sqlite_startup_migrates_and_resets_failed_only_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "concurrent.db"
    secret = "s" * 40
    initial = SettingsStore(str(path), secret)
    encrypted = initial.encrypt("ELASTIC-legacy-key")
    with initial._connect() as db:
        db.execute("DELETE FROM settings WHERE `key`='elastic_email_only_v1'")
        db.execute("DROP TABLE email_config")
        db.execute("""CREATE TABLE email_config(id INTEGER PRIMARY KEY,sendgrid_api_key_enc TEXT NOT NULL,
                   from_email TEXT NOT NULL,from_name TEXT NOT NULL,enabled INTEGER NOT NULL,
                   updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("INSERT INTO email_config(id,sendgrid_api_key_enc,from_email,from_name,enabled) VALUES(1,?,?,?,1)",
                   (encrypted, "verified@example.com", "Sender"))
        for fingerprint, status, attempts, error in (("failed", "failed", 5, "SendGrid HTTP 401"),
                                                      ("sent", "delivered", 1, None), ("pending", "pending", 2, None)):
            db.execute("INSERT INTO voicemail_deliveries(fingerprint,mailbox,recipient,status,attempts,last_error) VALUES(?,?,?,?,?,?)",
                       (fingerprint, "101", "agent@example.com", status, attempts, error))
        db.execute("CREATE TABLE retry_migration_audit(fingerprint TEXT)")
        db.execute("""CREATE TRIGGER track_migration_reset AFTER UPDATE OF attempts ON voicemail_deliveries
                      BEGIN INSERT INTO retry_migration_audit VALUES(new.fingerprint); END""")

    barrier = threading.Barrier(6)
    def start_worker(_):
        barrier.wait(timeout=10)
        return SettingsStore(str(path), secret).get_email_config(include_key=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(start_worker, range(6)))
    assert all(config["provider"] == "elasticemail" and config["api_key"] == "ELASTIC-legacy-key" for config in results)
    with initial._connect() as db:
        assert [row["fingerprint"] for row in db.execute("SELECT * FROM retry_migration_audit").fetchall()] == ["failed"]
        attempts = {row["fingerprint"]: (row["status"], row["attempts"]) for row in db.execute("SELECT fingerprint,status,attempts FROM voicemail_deliveries")}
        assert attempts == {"failed": ("failed", 0), "sent": ("delivered", 1), "pending": ("pending", 2)}
        assert db.execute("SELECT sendgrid_api_key_enc FROM email_config").fetchone()[0] == encrypted


def test_migration_retries_a_definite_401_but_not_accepted_or_unknown_messages(elastic_system, monkeypatch):
    settings, store, notifier = elastic_system
    directory = store.root / store.context / "101" / "INBOX"
    directory.mkdir(parents=True)
    for index in range(3):
        stem = f"msg{index:04d}"
        (directory / f"{stem}.txt").write_text(f"[message]\ncallerid=Customer\norigtime={1700000000 + index}\nduration=4\n")
        audio = directory / f"{stem}.wav"
        audio.write_bytes(f"RIFF-message-{index}".encode())
        os.utime(audio, (time.time() - 20, time.time() - 20))
    rows = {row["message"]: row for row in store.list_messages("101", "inbox")}
    identities = {stem: notifier._fingerprint(row, (directory / f"{stem}.wav").read_bytes())
                  for stem, row in rows.items()}
    with settings._connect() as db:
        db.execute("DELETE FROM settings WHERE `key`='elastic_email_only_v1'")
        db.execute("UPDATE email_config SET provider='sendgrid'")
        for stem, status, attempts, error in (("msg0000", "failed", 5, "SendGrid HTTP 401: Unauthorized"),
                                              ("msg0001", "delivered", 1, None),
                                              ("msg0002", "failed", 5, "SendGrid request timed out; acceptance is unknown")):
            db.execute("""INSERT INTO voicemail_deliveries(fingerprint,mailbox,recipient,status,attempts,last_error,updated_at)
                          VALUES(?,?,?,?,?,?,datetime('now','-2 minutes'))""",
                       (identities[stem], "101", "agent@example.com", status, attempts, error))
    upgraded = SettingsStore(str(settings.path), "s" * 40)
    sender = VoicemailEmailNotifier(store, upgraded)
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post",
                        lambda url, **kw: calls.append((url, kw)) or fake_response(200, {"success": True}))
    assert sender.process() == 1 and sender.process() == 0
    assert len(calls) == 1 and calls[0][0] == ELASTIC_EMAIL_URL
    assert calls[0][1]["data"]["apikey"] == "ELASTIC-secret"
    assert calls[0][1]["files"][0][1][1] == b"RIFF-message-0"
    after = {row["fingerprint"]: row for row in upgraded.list_voicemail_deliveries()}
    assert after[identities["msg0000"]]["status"] == "delivered"
    assert after[identities["msg0001"]]["status"] == "delivered" and after[identities["msg0001"]]["attempts"] == 1
    assert after[identities["msg0002"]]["status"] == "failed" and after[identities["msg0002"]]["attempts"] == 5
