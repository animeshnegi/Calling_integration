import base64
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.admin import SettingsStore
from app.database import metadata, reconcile_statements
from app.voicemail import VoicemailStore
from app.voicemail_email import SENDGRID_URL, TRANSCRIPTION_URL, VoicemailEmailNotifier


def test_sendgrid_voicemail_attachment_is_sent_once(tmp_path, monkeypatch):
    settings = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    settings.save_extension({
        "extension": "101", "sip_username": "101", "sip_password": "sip-secret",
        "voicemail_enabled": True, "voicemail_pin": "1234", "voicemail_email": "agent@example.com",
    })
    settings.save_email_config({
        "enabled": True, "api_key": "SG.test-secret", "from_email": "verified@example.com", "from_name": "Calls",
    })
    directory = tmp_path / "voicemail" / "engineerip" / "101" / "INBOX"
    directory.mkdir(parents=True)
    (directory / "msg0000.txt").write_text("[message]\ncallerid=Customer\norigtime=1700000000\nduration=15\n")
    audio = directory / "msg0000.wav"
    audio.write_bytes(b"RIFF-email-test")
    os.utime(audio, (time.time() - 20, time.time() - 20))
    sent = []

    class Response:
        status_code = 202

    def fake_post(url, **kwargs):
        sent.append((url, kwargs))
        return Response()

    monkeypatch.setattr("app.voicemail_email.requests.post", fake_post)
    notifier = VoicemailEmailNotifier(VoicemailStore(str(tmp_path / "voicemail")), settings)
    assert notifier.process() == 1
    assert notifier.process() == 0
    assert len(sent) == 1
    payload = sent[0][1]["json"]
    assert payload["personalizations"][0]["to"][0]["email"] == "agent@example.com"
    assert payload["attachments"][0]["filename"].endswith(".wav")
    assert sent[0][1]["headers"]["Authorization"] == "Bearer SG.test-secret"


def test_sendgrid_key_is_not_exposed(tmp_path):
    settings = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    settings.save_email_config({"enabled": True, "api_key": "SG.secret", "from_email": "verified@example.com"})
    public = settings.get_email_config()
    assert public["has_api_key"] is True
    assert "api_key" not in public


@pytest.fixture
def email_system(tmp_path):
    settings = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    settings.save_email_config({"enabled": True, "api_key": "SG.secret", "from_email": "verified@example.com"})
    settings.save_extension({"extension": "101", "sip_password": "sip-secret", "voicemail_enabled": True,
                             "voicemail_pin": "1234", "voicemail_email": "agent@example.com"})
    store = VoicemailStore(str(tmp_path / "voicemail"))
    return settings, store, VoicemailEmailNotifier(store, settings)


def message_file(store, mailbox="101", folder="INBOX", index=0, suffix="wav", content=b"RIFF-voice-test"):
    directory = store.root / store.context / mailbox / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"msg{index:04d}.txt").write_text("[message]\ncallerid=Customer <script>\norigtime=1700000000\nduration=15\n")
    audio = directory / f"msg{index:04d}.{suffix}"
    audio.write_bytes(content)
    os.utime(audio, (time.time() - 20, time.time() - 20))
    return audio


def response(status=202, **data):
    return SimpleNamespace(status_code=status, json=lambda: data)


def test_key_rotation_trim_blank_preservation_and_partial_save(email_system):
    settings, _, _ = email_system
    settings.save_email_config({"api_key": "  SG.replacement\r\n", "transcription_api_key": " sk.transcription  "})
    settings.save_email_config({"api_key": "", "transcription_api_key": "", "from_name": "New sender"})
    cfg = settings.get_email_config(include_key=True)
    assert cfg["api_key"] == "SG.replacement"
    assert cfg["transcription_api_key"] == "sk.transcription"
    assert cfg["enabled"] and cfg["from_email"] == "verified@example.com"
    public = settings.get_email_config()
    assert public["has_transcription_api_key"] and not public["transcription_enabled"]
    assert "api_key" not in public and "transcription_api_key" not in public
    with settings._connect() as db:
        row = dict(db.execute("SELECT * FROM email_config").fetchone())
    assert "SG.replacement" not in str(row) and "sk.transcription" not in str(row)
    assert "secret" not in str(settings.get_settings())


@pytest.mark.parametrize("patch", [{"api_key": "Bearer SG.foo"}, {"api_key": "SG.foo\nbar"},
                                   {"transcription_enabled": True}, {"enabled": "false"}, {"from_name": "X\nY"}, []])
def test_invalid_config_is_atomic(email_system, patch):
    settings, _, _ = email_system
    before = settings.get_email_config(include_key=True)
    with pytest.raises(ValueError):
        settings.save_email_config(patch)
    assert settings.get_email_config(include_key=True) == before


def test_unreadable_old_key_can_be_replaced(email_system):
    settings, _, notifier = email_system
    settings.secret_key = b"changed-secret"
    ok, error = notifier.send_test("test@example.com")
    assert not ok and "SECRET_KEY" in error
    settings.save_email_config({"api_key": "SG.new-key"})
    assert settings.get_email_config(include_key=True)["api_key"] == "SG.new-key"


def test_test_email_works_without_enabling_automatic_notifications(email_system, monkeypatch):
    settings, _, notifier = email_system
    settings.save_email_config({"enabled": False})
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda url, **kw: calls.append((url, kw)) or response())
    assert notifier.send_test("test@example.com") == (True, None)
    assert not settings.get_email_config()["enabled"]
    assert calls[0][0] == SENDGRID_URL and "attachments" not in calls[0][1]["json"]
    assert "does not test transcription" in calls[0][1]["json"]["content"][0]["value"]


@pytest.mark.parametrize("status, hint", [(400, "sender"), (401, "replacement"), (403, "Mail Send"),
                                         (429, "rate limit"), (503, "unavailable"), (302, "Unexpected")])
def test_sendgrid_errors_are_actionable_and_never_echo_provider_body(email_system, monkeypatch, status, hint):
    _, _, notifier = email_system
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda *a, **kw: response(status, errors="SG.secret sk.private <script>"))
    ok, error = notifier.send_test("test@example.com")
    assert not ok and hint in error
    assert "SG.secret" not in error and "<script>" not in error


def test_same_extension_digits_across_tenants_use_exact_mailboxes(tmp_path, monkeypatch):
    settings = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    settings.save_email_config({"enabled": True, "api_key": "SG.secret", "from_email": "verified@example.com"})
    settings.save_provider({"name": "p", "server": "sip.example.com", "username": "u", "password": "password", "allowed_ips": "198.51.100.10/32"})
    store = VoicemailStore(str(tmp_path / "voicemail"))
    for index, number in enumerate(("+13025550041", "+13025550042")):
        owner = settings.save_user({"username": f"tenant{index}", "email": f"tenant{index}@example.com", "password": "long-customer-password", "role": "user", "active": True})
        settings.save_number({"number": number, "provider": "p", "owner_user_id": owner, "active": True})
        settings.add_extension_to_number(number, owner, {"extension": "101", "voicemail_enabled": True,
                                        "voicemail_pin": "1234", "voicemail_email": f"tenant{index}@example.com"})
        message_file(store, "101-" + number[1:])
    # An ambiguous legacy box must not be guessed into either tenant.
    message_file(store, "101")
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda url, **kw: calls.append(kw["json"]) or response())
    notifier = VoicemailEmailNotifier(store, settings)
    assert notifier.process() == 2
    assert notifier.process() == 0
    recipients = {p["personalizations"][0]["to"][0]["email"]: p["attachments"][0]["filename"] for p in calls}
    assert "101-13025550041" in recipients["tenant0@example.com"]
    assert "101-13025550042" in recipients["tenant1@example.com"]


def test_transcript_and_audio_in_same_email_escaped_and_not_stored(email_system, monkeypatch):
    settings, store, notifier = email_system
    settings.save_email_config({"transcription_enabled": True, "transcription_api_key": "sk.transcription"})
    audio = message_file(store)
    calls = []
    def post(url, **kw):
        calls.append((url, kw))
        return response(200, text="Please call me.\n<script>private transcript</script>") if url == TRANSCRIPTION_URL else response()
    monkeypatch.setattr("app.voicemail_email.requests.post", post)
    assert notifier.process() == 1
    assert [url for url, _ in calls] == [TRANSCRIPTION_URL, SENDGRID_URL]
    upload = calls[0][1]
    assert upload["headers"]["Authorization"] == "Bearer sk.transcription"
    assert upload["data"] == {"model": "whisper-1"}
    assert upload["files"]["file"] == ("voicemail.wav", audio.read_bytes(), "audio/wav")
    assert upload["allow_redirects"] is False
    payload = calls[1][1]["json"]
    assert "Please call me." in payload["content"][0]["value"]
    assert "<script>" not in payload["content"][1]["value"]
    assert "&lt;script&gt;private transcript" in payload["content"][1]["value"]
    assert base64.b64decode(payload["attachments"][0]["content"]) == audio.read_bytes()
    deliveries = settings.list_voicemail_deliveries()
    assert deliveries[0]["transcription_status"] == "transcribed"
    assert "private transcript" not in str(deliveries)
    assert notifier.process() == 0 and len(calls) == 2


@pytest.mark.parametrize("failure, expected", [(401, "invalid_key"), (429, "quota_or_rate_limit"),
                                              (503, "provider_error"), ("empty", "empty_transcript"),
                                              ("timeout", "timeout"), ("invalid_json", "provider_error")])
def test_transcription_failure_still_sends_audio(email_system, monkeypatch, failure, expected):
    import requests
    settings, store, notifier = email_system
    settings.save_email_config({"transcription_enabled": True, "transcription_api_key": "sk.transcription"})
    message_file(store)
    sent = []
    def post(url, **kw):
        if url == TRANSCRIPTION_URL:
            if failure == "timeout":
                raise requests.Timeout("do not disclose sk.transcription")
            if failure == "invalid_json":
                return SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError("bad JSON")))
            return response(200) if failure == "empty" else response(failure)
        sent.append(kw["json"])
        return response()
    monkeypatch.setattr("app.voicemail_email.requests.post", post)
    assert notifier.process() == 1
    assert len(sent) == 1 and sent[0]["attachments"]
    assert "Transcription is unavailable" in sent[0]["content"][0]["value"]
    assert settings.list_voicemail_deliveries()[0]["transcription_status"] == expected


@pytest.mark.parametrize("enabled,suffix,expected", [(False, "wav", "disabled"), (True, "gsm", "unsupported_audio")])
def test_disabled_or_unsupported_transcription_never_uploads_audio(email_system, monkeypatch, enabled, suffix, expected):
    settings, store, notifier = email_system
    settings.save_email_config({"transcription_enabled": enabled, "transcription_api_key": "sk.transcription"})
    message_file(store, suffix=suffix)
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda url, **kw: calls.append(url) or response())
    assert notifier.process() == 1 and calls == [SENDGRID_URL]
    assert settings.list_voicemail_deliveries()[0]["transcription_status"] == expected


def test_unreadable_transcription_key_does_not_prevent_audio_email(email_system, monkeypatch):
    settings, store, notifier = email_system
    settings.save_email_config({"transcription_enabled": True, "transcription_api_key": "sk.old"})
    settings.secret_key = b"new-secret"
    settings.save_email_config({"api_key": "SG.new"})
    message_file(store)
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda url, **kw: calls.append(url) or response())
    assert notifier.process() == 1 and calls == [SENDGRID_URL]
    assert settings.list_voicemail_deliveries()[0]["transcription_status"] == "key_unavailable"


def test_recipient_is_preserved_when_editing_other_extension_fields(email_system):
    settings, _, _ = email_system
    settings.save_extension({"extension": "101", "display_name": "New label"})
    assert settings.list_extensions()[0]["voicemail_email"] == "agent@example.com"
    settings.save_extension({"extension": "101", "voicemail_email": ""})
    assert not settings.list_extensions()[0]["voicemail_email"]


def test_claim_is_atomic_has_backoff_and_key_repair_retries_failures(email_system):
    settings, _, _ = email_system
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _: settings.claim_voicemail_delivery("fp", "101", "a@example.com"), range(4)))
    assert sum(claimed) == 1
    assert not settings.claim_voicemail_delivery("fp", "101", "a@example.com")
    settings.finish_voicemail_delivery("fp", False, "SendGrid HTTP 401")
    assert not settings.claim_voicemail_delivery("fp", "101", "a@example.com")
    with settings._connect() as db:
        db.execute("UPDATE voicemail_deliveries SET attempts=5,updated_at=datetime('now','-2 minutes') WHERE fingerprint='fp'")
    assert not settings.claim_voicemail_delivery("fp", "101", "a@example.com")
    settings.save_email_config({"api_key": ""})  # Keeping a key is not a repair.
    assert settings.list_voicemail_deliveries()[0]["attempts"] == 5
    settings.save_email_config({"api_key": "SG.repaired"})
    assert settings.claim_voicemail_delivery("fp", "101", "new@example.com")
    settings.finish_voicemail_delivery("fp", True)
    settings.save_email_config({"api_key": "SG.another"})
    assert not settings.claim_voicemail_delivery("fp", "101", "new@example.com")
    assert settings.list_voicemail_deliveries()[0]["recipient"] == "new@example.com"


def test_background_scan_is_nonblocking_nonoverlapping_and_recovers(email_system, monkeypatch):
    _, _, notifier = email_system
    started, release = threading.Event(), threading.Event()
    def process():
        started.set()
        assert release.wait(2)
        raise RuntimeError("broken message scan")
    monkeypatch.setattr(notifier, "process", process)
    assert notifier.process_background()
    assert started.wait(1)
    assert not notifier.process_background()
    release.set()
    notifier._thread.join(2)
    assert not notifier._thread.is_alive()
    monkeypatch.setattr(notifier, "process", lambda: 0)
    assert notifier.process_background()
    notifier._thread.join(2)


def test_existing_sqlite_email_schema_upgrades_idempotently(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE email_config(id INTEGER PRIMARY KEY,sendgrid_api_key_enc TEXT NOT NULL,
                         from_email TEXT NOT NULL,from_name TEXT NOT NULL,enabled INTEGER NOT NULL,
                         updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                         CREATE TABLE voicemail_deliveries(fingerprint TEXT PRIMARY KEY,mailbox TEXT NOT NULL,recipient TEXT NOT NULL,
                         status TEXT NOT NULL,attempts INTEGER NOT NULL,last_error TEXT,delivered_at TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);""")
        db.execute("INSERT INTO email_config(id,sendgrid_api_key_enc,from_email,from_name,enabled) VALUES(1,'','old@example.com','Old',0)")
    for _ in range(2):
        settings = SettingsStore(str(path), "s" * 40)
        assert settings.get_email_config()["from_email"] == "old@example.com"
        assert not settings.get_email_config()["transcription_enabled"]


def test_mysql_additive_transcription_upgrade_has_safe_defaults():
    existing = {t.name: {c.name: {"type": c.type} for c in t.columns} for t in metadata.sorted_tables}
    for table, column in (("email_config", "transcription_enabled"), ("email_config", "transcription_api_key_enc"),
                          ("voicemail_deliveries", "transcription_status")):
        del existing[table][column]
    ddl = "\n".join(reconcile_statements(existing))
    assert "ADD COLUMN transcription_enabled INTEGER NOT NULL DEFAULT '0'" in ddl
    assert "ADD COLUMN transcription_api_key_enc VARCHAR(2048) NOT NULL DEFAULT ''" in ddl
    assert "ADD COLUMN transcription_status VARCHAR(20) NOT NULL DEFAULT 'disabled'" in ddl


def test_distinct_calls_with_identical_audio_both_notify_and_renumber_does_not_duplicate(email_system, monkeypatch):
    settings, store, notifier = email_system
    message_file(store, index=0)
    second = message_file(store, index=1)
    second.with_suffix('.txt').write_text("[message]\ncallerid=Customer\norigtime=1700000001\nduration=15\n")
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda url, **kw: calls.append(kw["json"]) or response())
    assert notifier.process() == 2 and len(calls) == 2
    # A read moves msg0000 to Old and renumbers the other msg to msg0000.
    store.mark_read("101", "inbox", "msg0000")
    assert notifier.process() == 0 and len(calls) == 2


def test_legacy_accepted_audio_not_resent_but_later_identical_call_is_eligible(email_system, monkeypatch):
    import hashlib
    settings, store, notifier = email_system
    audio = message_file(store)
    old_id = hashlib.sha256(b"101" + audio.read_bytes()).hexdigest()
    assert settings.claim_voicemail_delivery(old_id, "101", "agent@example.com")
    settings.finish_voicemail_delivery(old_id, True)
    calls = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda url, **kw: calls.append(kw["json"]) or response())
    assert notifier.process() == 0 and not calls
    # A later call with the same audio is not the historical accepted message.
    audio.with_suffix('.txt').write_text(f"[message]\ncallerid=Customer\norigtime={int(time.time()) + 1}\nduration=15\n")
    assert notifier.process() == 1 and len(calls) == 1
    assert notifier.process() == 0


@pytest.mark.parametrize("exc", ["timeout", "connection"])
def test_sendgrid_network_errors_are_sanitized(email_system, monkeypatch, exc):
    import requests
    settings, _, notifier = email_system
    def fail(*args, **kwargs):
        kind = requests.Timeout if exc == "timeout" else requests.ConnectionError
        raise kind("SG.secret do not expose this")
    monkeypatch.setattr("app.voicemail_email.requests.post", fail)
    ok, error = notifier.send_test("test@example.com")
    assert not ok and "SG.secret" not in error
    assert "timed out" in error if exc == "timeout" else "Cannot reach" in error


def test_recent_oversized_inactive_and_old_messages_are_not_sent(email_system, monkeypatch):
    import app.voicemail_email as module
    settings, store, notifier = email_system
    audio = message_file(store)
    calls = []
    monkeypatch.setattr(module.requests, "post", lambda url, **kw: calls.append(url) or response())
    os.utime(audio, None)
    assert notifier.process() == 0  # Asterisk may still be writing.
    os.utime(audio, (time.time() - 20, time.time() - 20))
    monkeypatch.setattr(module, "MAX_ATTACHMENT_BYTES", 1)
    assert notifier.process() == 0
    monkeypatch.setattr(module, "MAX_ATTACHMENT_BYTES", 20 * 1024 * 1024)
    settings.save_extension({"extension": "101", "active": False})
    assert notifier.process() == 0
    settings.save_extension({"extension": "101", "active": True})
    store.mark_read("101", "inbox", "msg0000")
    assert notifier.process() == 0 and not calls


def test_abandoned_pending_claim_can_be_recovered(email_system):
    settings, _, _ = email_system
    assert settings.claim_voicemail_delivery("stale", "101", "a@example.com")
    with settings._connect() as db:
        db.execute("UPDATE voicemail_deliveries SET updated_at=datetime('now','-11 minutes')")
    assert settings.claim_voicemail_delivery("stale", "101", "a@example.com")
    assert settings.list_voicemail_deliveries()[0]["attempts"] == 2
