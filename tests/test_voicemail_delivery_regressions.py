"""Regressions for real mailbox provisioning, email selection, and disk access."""
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.admin import SettingsStore
from app.telephony_config import TelephonyConfigSync
from app.voicemail import VoicemailStore
from app.voicemail_email import VoicemailEmailNotifier


def setup_customer(tmp_path, monkeypatch, *, address="owner@example.com"):
    # Everything configured 60 seconds ago; messages below are newer, but old
    # enough to have finished recording when the worker scans them.
    now = time.time()
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: now - 60)
        settings = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
        settings.save_email_config({"enabled": True, "api_key": "ELASTIC.test", "from_email": "verified@example.com"})
        settings.save_provider({"name": "p", "server": "sip.example.com", "username": "u", "password": "password", "allowed_ips": "198.51.100.10/32"})
        owner = settings.save_user({"username": "tenant", "email": address, "password": "long-customer-password", "role": "user", "active": True})
        number = "+13025559907"
        settings.save_number({"number": number, "provider": "p", "owner_user_id": owner, "active": True})
        provisioned = settings.provision_number(owner, number)
    voicemail = VoicemailStore(str(tmp_path / "voicemail"))
    return settings, voicemail, owner, number, provisioned["extension"]


def record(store, mailbox, *, modified=None, index=0):
    inbox = store.root / store.context / mailbox / "INBOX"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"msg{index:04d}.txt").write_text("[message]\ncallerid=Customer\norigtime=1700000000\nduration=5\n")
    audio = inbox / f"msg{index:04d}.wav"
    audio.write_bytes(b"RIFF-test-voicemail")
    modified = time.time() - 20 if modified is None else modified
    os.utime(audio, (modified, modified))
    return audio


def fake_elastic(monkeypatch):
    sent = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda url, **kw: sent.append((url, kw)) or SimpleNamespace(status_code=200, json=lambda: {"success": True}))
    return sent


def test_flow_voicemail_sends_even_when_desk_switch_is_off(tmp_path, monkeypatch):
    settings, vm, owner, number, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_email": "custom@example.com"}, owner)
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": "ring_group", "extensions": [key], "timeout": 25, "configured": True},
        {"type": "voicemail", "mailbox": key, "configured": True},
    ]}})
    extension = next(x for x in settings.list_extensions() if x["key"] == key)
    assert not extension["voicemail_enabled"]
    assert key in TelephonyConfigSync(settings, None, str(tmp_path / "dynamic.conf")).voicemail_keys([extension])
    record(vm, extension["mailbox"])
    sent = fake_elastic(monkeypatch)
    assert VoicemailEmailNotifier(vm, settings).process() == 1
    assert sent[0][1]["data"]["to"] == "custom@example.com"


def test_provisioned_mailbox_uses_its_associated_account_email(tmp_path, monkeypatch):
    settings, vm, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234"}, owner)
    extension = next(x for x in settings.list_extensions() if x["key"] == key)
    assert not extension["voicemail_email"]  # Provisioning left no explicit address.
    record(vm, extension["mailbox"])
    sent = fake_elastic(monkeypatch)
    assert VoicemailEmailNotifier(vm, settings).process() == 1
    assert sent[0][1]["data"]["to"] == "owner@example.com"


def test_listing_existing_inbox_does_not_need_to_create_missing_folders(tmp_path, monkeypatch):
    vm = VoicemailStore(str(tmp_path / "voicemail"))
    record(vm, "101")
    original = Path.mkdir
    def deny_extra_folders(path, *args, **kwargs):
        if path.name in {"Old", "Urgent"}:
            raise PermissionError("mailbox directory is not group writable")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", deny_extra_folders)
    assert [m["message"] for m in vm.list_messages()] == ["msg0000"]
    assert not (vm.root / vm.context / "101" / "Old").exists()


def test_automated_sender_notice_on_voicemail_and_test_email(tmp_path, monkeypatch):
    settings, vm, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234", "voicemail_email": "custom@example.com"}, owner)
    extension = next(x for x in settings.list_extensions() if x["key"] == key)
    record(vm, extension["mailbox"])
    sent = fake_elastic(monkeypatch)
    notifier = VoicemailEmailNotifier(vm, settings)
    assert notifier.process() == 1
    assert notifier.send_test("test@example.com") == (True, None)
    for _, request in sent:
        for field in ("bodyText", "bodyHtml"):
            body = request["data"][field].lower()
            assert "automated" in body and "do not reply" in body
            assert "not associated with a user" in body


def test_legacy_blank_auto_migration_sends_new_not_old_and_cutover_survives_restart(tmp_path, monkeypatch):
    settings, vm, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234"}, owner)
    mailbox = settings.list_extensions(owner)[0]["mailbox"]
    now = time.time()
    record(vm, mailbox, modified=now - 100, index=0)
    record(vm, mailbox, modified=now - 20, index=1)
    # This is the real pre-upgrade SQLite schema: the previously blank email has
    # no mode or per-extension timestamp, and there is no global cutover marker.
    with settings._connect() as db:
        db.execute("ALTER TABLE extensions DROP COLUMN voicemail_email_mode")
        db.execute("ALTER TABLE extensions DROP COLUMN voicemail_email_auto_since_ms")
        db.execute("ALTER TABLE extensions DROP COLUMN voicemail_email_resume_since_ms")
        db.execute("DELETE FROM settings WHERE `key`='voicemail_associated_email_since_ms'")
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: now - 60)
        upgraded = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    cutover = upgraded.get_settings()["voicemail_associated_email_since_ms"]
    assert upgraded.list_extensions(owner)[0]["voicemail_email_mode"] == "auto"
    # Another worker starting later must not move the cutover forward.
    restarted = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    assert restarted.get_settings()["voicemail_associated_email_since_ms"] == cutover
    sent = fake_elastic(monkeypatch)
    notifier = VoicemailEmailNotifier(vm, restarted)
    assert notifier.process() == 1
    assert len(sent) == 1 and "msg0001" in sent[0][1]["files"][0][1][0]
    assert notifier.process() == 0
    assert len(vm.list_messages(mailbox)) == 2  # No messages are deleted or moved.


def test_explicit_opt_out_and_reenable_never_send_auto_recipient_backlog(tmp_path, monkeypatch):
    settings, vm, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234"}, owner)
    mailbox = settings.list_extensions(owner)[0]["mailbox"]
    now = time.time()
    record(vm, mailbox, modified=now - 50, index=0)
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: now - 40)
        # Legacy clients express an opt-out by explicitly clearing the address.
        settings.save_extension({"extension": key, "voicemail_email": ""}, owner)
    assert settings.list_extensions(owner)[0]["voicemail_email_mode"] == "off"
    record(vm, mailbox, modified=now - 35, index=1)
    sent = fake_elastic(monkeypatch)
    notifier = VoicemailEmailNotifier(vm, settings)
    assert notifier.process() == 0 and not sent
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: now - 30)
        settings.save_extension({"extension": key, "voicemail_email_mode": "auto", "voicemail_email": ""}, owner)
    record(vm, mailbox, modified=now - 15, index=2)
    assert notifier.process() == 1
    assert len(sent) == 1 and "msg0002" in sent[0][1]["files"][0][1][0]
    assert sent[0][1]["data"]["to"] == "owner@example.com"


def test_explicit_address_wins_and_profile_edit_does_not_change_or_enable_it(tmp_path, monkeypatch):
    settings, vm, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234",
                             "voicemail_email": "desk@example.com"}, owner)
    # A full key in the user profile must still not overwrite a custom recipient.
    settings.save_user({"id": owner, "username": "tenant", "email": "owner@example.com", "extension": key,
                        "role": "user", "active": True})
    settings.update_user_email(owner, "new-owner@example.com")
    assert settings.list_extensions(owner)[0]["voicemail_email"] == "desk@example.com"
    mailbox = settings.list_extensions(owner)[0]["mailbox"]
    record(vm, mailbox)
    sent = fake_elastic(monkeypatch)
    notifier = VoicemailEmailNotifier(vm, settings)
    assert notifier.process() == 1 and sent[0][1]["data"]["to"] == "desk@example.com"
    settings.save_extension({"extension": key, "voicemail_email_mode": "off"}, owner)
    settings.update_user_email(owner, "newer-owner@example.com")
    assert settings.list_extensions(owner)[0]["voicemail_email_mode"] == "off"
    new = record(vm, mailbox, index=1)
    new.write_bytes(b"RIFF-different-audio")
    os.utime(new, (time.time() - 20, time.time() - 20))
    assert notifier.process() == 0 and len(sent) == 1


def test_owner_email_follows_profile_change_without_converting_to_override(tmp_path, monkeypatch):
    settings, vm, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234"}, owner)
    settings.save_user({"id": owner, "username": "tenant", "email": "changed@example.com", "extension": key,
                        "role": "user", "active": True})
    settings.update_user_email(owner, "latest@example.com")
    extension = settings.list_extensions(owner)[0]
    assert extension["voicemail_email"] == "" and extension["voicemail_email_mode"] == "auto"
    record(vm, extension["mailbox"])
    sent = fake_elastic(monkeypatch)
    assert VoicemailEmailNotifier(vm, settings).process() == 1
    assert sent[0][1]["data"]["to"] == "latest@example.com"


def test_automatic_recipients_are_number_scoped_and_inactive_owners_are_skipped(tmp_path, monkeypatch):
    settings, vm, owner_a, number_a, key_a = setup_customer(tmp_path, monkeypatch, address="first@example.com")
    with monkeypatch.context() as m:
        now = time.time()
        m.setattr(time, "time", lambda: now - 60)
        owner_b = settings.save_user({"username": "second", "email": "second@example.com", "password": "long-customer-password", "role": "user", "active": True})
        number_b = "+13025559908"
        settings.save_number({"number": number_b, "provider": "p", "owner_user_id": owner_b, "active": True})
        key_b = settings.provision_number(owner_b, number_b)["extension"]
        for owner, key in ((owner_a, key_a), (owner_b, key_b)):
            settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234"}, owner)
    for row in settings.list_extensions():
        record(vm, row["mailbox"])
    record(vm, "101")  # Ambiguous legacy digits alone must never be guessed.
    sent = fake_elastic(monkeypatch)
    notifier = VoicemailEmailNotifier(vm, settings)
    assert notifier.process() == 2
    by_mailbox = {kw["files"][0][1][0].split("-msg")[0]: kw["data"]["to"] for _, kw in sent}
    assert by_mailbox == {"voicemail-101-13025559907": "first@example.com", "voicemail-101-13025559908": "second@example.com"}
    # Newly recorded messages for a disabled account do not use its owner email.
    settings.save_user({"id": owner_b, "username": "second", "email": "second@example.com", "role": "user", "active": False})
    new = record(vm, "101-13025559908", index=1)
    new.write_bytes(b"RIFF-different")
    os.utime(new, (time.time() - 20, time.time() - 20))
    assert notifier.process() == 0 and len(sent) == 2


@pytest.mark.parametrize("platform_active", [0, 1])
def test_digits_only_profile_cannot_overwrite_another_number_or_platform_mailbox(tmp_path, monkeypatch, platform_active):
    settings, _, owner, number, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_email": "owner@example.com"}, owner)
    # Both addresses equal the old profile email, so an incorrectly chosen row
    # would be rewritten. An inactive duplicate is still ambiguous.
    with settings._connect() as db:
        db.execute("INSERT INTO extensions(extension,sip_username,sip_password_enc,voicemail_email,active) VALUES(?,?,?,?,?)",
                   ("101", "PLATFORM_101", settings.encrypt("secret"), "owner@example.com", platform_active))
        db.execute("UPDATE admin_users SET extension='101' WHERE id=?", (owner,))
    settings.update_user_email(owner, "changed@example.com")
    rows = {row["key"]: row for row in settings.list_extensions()}
    assert rows["101"]["voicemail_email"] == "owner@example.com"
    assert rows[key]["voicemail_email"] == "owner@example.com"


def test_flow_with_disabled_voicemail_step_does_not_enable_notifications(tmp_path, monkeypatch):
    settings, vm, owner, number, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_email": "desk@example.com"}, owner)
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": "ring_group", "extensions": [key], "timeout": 25, "configured": True},
        {"type": "voicemail", "mailbox": key, "enabled": False, "configured": True},
    ]}})
    record(vm, settings.list_extensions(owner)[0]["mailbox"])
    sent = fake_elastic(monkeypatch)
    assert VoicemailEmailNotifier(vm, settings).process() == 0 and not sent


def test_ownerless_mailbox_uses_exact_profile_new_messages_only_and_never_guesses(tmp_path, monkeypatch):
    now = time.time()
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: now - 100)
        settings = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
        settings.save_email_config({"enabled": True, "api_key": "ELASTIC.test", "from_email": "sender@example.com"})
        settings.save_extension({"extension": "101", "voicemail_enabled": True, "voicemail_pin": "1234"})
    vm = VoicemailStore(str(tmp_path / "voicemail"))
    record(vm, "101", modified=now - 80, index=0)
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: now - 40)
        user = settings.save_user({"username": "attached", "email": "attached@example.com", "extension": "101",
                                   "password": "long-customer-password", "role": "user", "active": True})
    assert settings.list_extensions()[0]["voicemail_email"] == ""  # Not converted into a custom override.
    record(vm, "101", modified=now - 20, index=1)
    sent = fake_elastic(monkeypatch)
    notifier = VoicemailEmailNotifier(vm, settings)
    assert notifier.process() == 1
    assert len(sent) == 1 and sent[0][1]["data"]["to"] == "attached@example.com"
    assert "msg0001" in sent[0][1]["files"][0][1][0]
    settings.update_user_email(user, "new-attached@example.com")
    assert settings.list_extensions()[0]["voicemail_email"] == ""
    # Two profiles attached to a platform mailbox are ambiguous, not a reason
    # to send private recordings to whichever profile happens to sort first.
    settings.save_user({"username": "also-attached", "email": "other@example.com", "extension": "101",
                        "password": "long-customer-password", "role": "user", "active": True})
    newer = record(vm, "101", modified=now - 15, index=2)
    newer.write_bytes(b"RIFF-new-audio")
    os.utime(newer, (now - 15, now - 15))
    assert notifier.process() == 0 and len(sent) == 1


def test_mismatched_line_ownership_never_sends_even_to_an_old_custom_address(tmp_path, monkeypatch):
    settings, vm, owner, number, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234",
                             "voicemail_email": "former-tenant@example.com"}, owner)
    second = settings.save_user({"username": "second-owner", "email": "second@example.com",
                                 "password": "long-customer-password", "role": "user", "active": True})
    with settings._connect() as db:
        db.execute("UPDATE phone_numbers SET owner_user_id=? WHERE number=?", (second, number))
    record(vm, "101-13025559907")
    sent = fake_elastic(monkeypatch)
    assert VoicemailEmailNotifier(vm, settings).process() == 0 and not sent


def test_identity_migration_keeps_a_profile_full_scoped_key(tmp_path, monkeypatch):
    settings, _, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_user({"id": owner, "username": "tenant", "email": "owner@example.com", "extension": key,
                        "role": "user", "active": True})
    with settings._connect() as db:
        db.execute("DELETE FROM settings WHERE `key`=?", (settings.EXTENSION_IDENTITY_FLAG,))
    migrated = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    assert next(user for user in migrated.list_users() if user["id"] == owner)["extension"] == key


def test_associated_recipient_retries_failures_without_resending_acceptances(tmp_path, monkeypatch):
    settings, vm, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234"}, owner)
    record(vm, settings.list_extensions(owner)[0]["mailbox"])
    responses = [SimpleNamespace(status_code=503, json=lambda: {"success": False}),
                 SimpleNamespace(status_code=200, json=lambda: {"success": True})]
    sent = []
    monkeypatch.setattr("app.voicemail_email.requests.post", lambda url, **kw: sent.append(kw) or responses.pop(0))
    notifier = VoicemailEmailNotifier(vm, settings)
    assert notifier.process() == 0 and len(sent) == 1
    assert notifier.process() == 0 and len(sent) == 1  # Backoff is respected.
    settings.save_email_config({"api_key": "ELASTIC.corrected"})  # Reset failed attempt limit, not backoff.
    assert notifier.process() == 0 and len(sent) == 1
    with settings._connect() as db:
        db.execute("UPDATE voicemail_deliveries SET updated_at=datetime('now','-2 minutes') WHERE status='failed'")
    assert notifier.process() == 1 and notifier.process() == 0
    assert [item["data"]["to"] for item in sent] == ["owner@example.com", "owner@example.com"]
    assert settings.list_voicemail_deliveries()[0]["status"] == "delivered"


def test_invalid_mode_is_rejected_without_changing_the_recipient(tmp_path, monkeypatch):
    import pytest
    settings, _, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_email": "desk@example.com"}, owner)
    before = settings.list_extensions(owner)[0]
    with pytest.raises(ValueError):
        settings.save_extension({"extension": key, "voicemail_email_mode": "unexpected"}, owner)
    after = settings.list_extensions(owner)[0]
    assert (after["voicemail_email"], after["voicemail_email_mode"]) == (before["voicemail_email"], before["voicemail_email_mode"])


def test_one_disappearing_recording_does_not_abort_the_mailbox_scan(tmp_path, monkeypatch):
    vm = VoicemailStore(str(tmp_path / "voicemail"))
    record(vm, "101", index=0)
    record(vm, "101", index=1)
    original = vm._audio_path
    def removed_after_discovery(folder, stem):
        audio = original(folder, stem)
        if audio and stem == "msg0000":
            audio.unlink()
        return audio
    monkeypatch.setattr(vm, "_audio_path", removed_after_discovery)
    assert [message["message"] for message in vm.list_messages()] == ["msg0001"]


@pytest.mark.parametrize("change", ["extension_opt_out", "account_email", "line_owner", "email_disabled", "voicemail_disabled"])
def test_mail_settings_are_rechecked_after_slow_transcription(tmp_path, monkeypatch, change):
    settings, vm, owner, number, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234"}, owner)
    settings.save_email_config({"transcription_enabled": True})
    second = settings.save_user({"username": "second", "email": "second@example.com",
                                 "password": "long-customer-password", "role": "user", "active": True})
    record(vm, settings.list_extensions(owner)[0]["mailbox"])
    sent = fake_elastic(monkeypatch)
    notifier = VoicemailEmailNotifier(vm, settings)
    def during_decode(*args):
        if change == "extension_opt_out":
            settings.save_extension({"extension": key, "voicemail_email_mode": "off"}, owner)
        elif change == "account_email":
            settings.update_user_email(owner, "changed@example.com")
        elif change == "line_owner":
            with settings._connect() as db:
                db.execute("UPDATE phone_numbers SET owner_user_id=? WHERE number=?", (second, number))
        elif change == "email_disabled":
            settings.save_email_config({"enabled": False})
        else:
            settings.save_extension({"extension": key, "voicemail_enabled": False}, owner)
        return "public fixture words", "transcribed"
    monkeypatch.setattr(notifier, "_transcribe", during_decode)
    assert notifier.process() == 0 and not sent
    delivery = settings.list_voicemail_deliveries()[0]
    assert delivery["status"] == "failed" and "no email was sent" in delivery["last_error"]


def test_disabling_transcription_during_decode_sends_audio_without_text(tmp_path, monkeypatch):
    settings, vm, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234"}, owner)
    settings.save_email_config({"transcription_enabled": True})
    record(vm, settings.list_extensions(owner)[0]["mailbox"])
    sent = fake_elastic(monkeypatch)
    notifier = VoicemailEmailNotifier(vm, settings)
    def during_decode(*args):
        settings.save_email_config({"transcription_enabled": False})
        return "private recognized words", "transcribed"
    monkeypatch.setattr(notifier, "_transcribe", during_decode)
    assert notifier.process() == 1
    assert "private recognized words" not in sent[0][1]["data"]["bodyText"]
    assert "private recognized words" not in sent[0][1]["data"]["bodyHtml"]
    assert sent[0][1]["files"] and settings.list_voicemail_deliveries()[0]["transcription_status"] == "disabled"


def test_reenabling_a_custom_recipient_does_not_send_the_opt_out_backlog(tmp_path, monkeypatch):
    settings, vm, owner, _, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234",
                             "voicemail_email": "desk@example.com"}, owner)
    mailbox = settings.list_extensions(owner)[0]["mailbox"]
    now = time.time()
    record(vm, mailbox, modified=now - 50, index=0)
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: now - 40)
        settings.save_extension({"extension": key, "voicemail_email_mode": "off"}, owner)
    record(vm, mailbox, modified=now - 35, index=1)
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: now - 30)
        settings.save_extension({"extension": key, "voicemail_email_mode": "auto"}, owner)
    record(vm, mailbox, modified=now - 15, index=2)
    restarted = SettingsStore(str(tmp_path / "settings.db"), "s" * 40)
    sent = fake_elastic(monkeypatch)
    assert VoicemailEmailNotifier(vm, restarted).process() == 1
    assert len(sent) == 1 and sent[0][1]["data"]["to"] == "desk@example.com"
    assert "msg0002" in sent[0][1]["files"][0][1][0]
    assert len(vm.list_messages(mailbox)) == 3


def test_number_owner_changes_cut_off_old_audio_for_custom_recipients(tmp_path, monkeypatch):
    settings, vm, owner, number, key = setup_customer(tmp_path, monkeypatch)
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "1234",
                             "voicemail_email": "desk@example.com"}, owner)
    other = settings.save_user({"username": "other-owner", "email": "other@example.com",
                                "password": "long-customer-password", "role": "user", "active": True})
    mailbox = settings.list_extensions(owner)[0]["mailbox"]
    now = time.time()
    record(vm, mailbox, modified=now - 50, index=0)
    for changed_at, number_owner in ((now - 40, other), (now - 30, owner)):
        with monkeypatch.context() as m:
            m.setattr(time, "time", lambda: changed_at)
            settings.save_number({"number": number, "provider": "p", "owner_user_id": number_owner, "active": True})
    record(vm, mailbox, modified=now - 15, index=1)
    sent = fake_elastic(monkeypatch)
    assert VoicemailEmailNotifier(vm, settings).process() == 1
    assert len(sent) == 1 and sent[0][1]["data"]["to"] == "desk@example.com"
    assert "msg0001" in sent[0][1]["files"][0][1][0]
