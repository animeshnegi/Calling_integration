import os
import time

from app.admin import SettingsStore
from app.voicemail import VoicemailStore
from app.voicemail_email import VoicemailEmailNotifier


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
