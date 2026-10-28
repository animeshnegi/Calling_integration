"""Elastic Email v2 contract, provider switching and image-only DB upgrades."""
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
        "bodyText": "Your EngineerIP email configuration is working. This test contains no voicemail audio and does not test transcription.",
        "bodyHtml": "<p>Your EngineerIP email configuration is working.</p><p>This test contains no voicemail audio and does not test transcription.</p>",
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


def test_legacy_key_upgrade_and_provider_switch_are_safe(tmp_path):
    path = tmp_path / "old.db"
    secret = "s" * 40
    encryption_key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    # This might be a SendGrid key or an Elastic key entered in the old UI; the
    # schema cannot infer which. Do not send it to another provider automatically.
    ciphertext = Fernet(encryption_key).encrypt(b"ELASTIC-legacy-key").decode()
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE email_config(id INTEGER PRIMARY KEY,sendgrid_api_key_enc TEXT NOT NULL,
                     from_email TEXT NOT NULL,from_name TEXT NOT NULL,enabled INTEGER NOT NULL,
                     updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("INSERT INTO email_config(id,sendgrid_api_key_enc,from_email,from_name,enabled) VALUES(1,?,?,?,1)",
                   (ciphertext, "verified@example.com", "Old sender"))
    for _ in range(2):
        settings = SettingsStore(str(path), secret)
        existing = settings.get_email_config(include_key=True)
        assert existing["provider"] == "sendgrid"
        assert existing["api_key"] == "ELASTIC-legacy-key" and existing["enabled"]
    before = settings.get_email_config(include_key=True)
    with pytest.raises(ValueError, match="Enter the Elastic Email API key"):
        settings.save_email_config({"provider": "elasticemail", "api_key": ""})
    assert settings.get_email_config(include_key=True) == before
    settings.save_email_config({"provider": "elasticemail", "api_key": "ELASTIC-legacy-key"})
    settings.save_email_config({"api_key": "", "from_name": "New sender"})
    current = settings.get_email_config(include_key=True)
    assert current["provider"] == "elasticemail" and current["api_key"] == "ELASTIC-legacy-key"
    assert current["enabled"] and current["from_email"] == "verified@example.com"
    assert "api_key" not in settings.get_email_config()
    with settings._connect() as db:
        assert "ELASTIC-legacy-key" not in str(dict(db.execute("SELECT * FROM email_config").fetchone()))
    for bad in ("", "unknown", "https://evil.example", None):
        with pytest.raises(ValueError, match="Choose SendGrid or Elastic Email"):
            settings.save_email_config({"provider": bad, "api_key": "another-key"})
        assert settings.get_email_config(include_key=True) == current


def test_mysql_upgrade_adds_provider_without_changing_legacy_key():
    existing = {t.name: {c.name: {"type": c.type} for c in t.columns} for t in metadata.sorted_tables}
    del existing["email_config"]["provider"]
    ddl = "\n".join(reconcile_statements(existing))
    assert "ADD COLUMN provider VARCHAR(20) NOT NULL DEFAULT 'sendgrid'" in ddl
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
