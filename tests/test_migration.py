"""The one-time migration onto number-scoped extensions.

An existing install - rows carrying bare digits, a number whose inbound link
names those digits, device accounts keyed by digits, recordings, voicemail and a
customer call default - has to come out the other side with every row on the
right phone number, nothing merged and nothing lost.
"""
import sqlite3

from app.admin import SettingsStore
from app.telephony_config import TelephonyConfigSync


def legacy_database(path, secret="a" * 40):
    """Build the database shape an install used before this change.

    Everything is written the modern way first (so the passwords are encrypted
    with the same key the store will use), then the extensions table is put back
    into its pre-identity-v2 shape and the references are written the way that
    release wrote them: bare digits.
    """
    store = SettingsStore(str(path), secret)
    store.save_provider({
        "name": "Carrier", "server": "sip.example.com", "username": "user", "password": "secret",
        "allowed_ips": "198.51.100.10/32", "codecs": "ulaw,alaw",
    })
    store.save_user({"username": "meridian", "password": "customer-password-1", "role": "user",
                     "email": "m@example.com", "company_name": "Meridian"})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    for number, inbound in (("+13025550001", "101"), ("+13025550002", "102"), ("+13025550098", "101")):
        store.save_number({
            "number": number, "provider": "Carrier", "owner_user_id": meridian, "active": True,
            "inbound_extension": inbound,
        })
    # 101 on the first number and 101 on the third are two desks. 102 answers the
    # second number. 105 is a desk whose number never named it. 900 is the
    # operator's own, with no customer and no line.
    first_101 = store.add_extension_to_number("+13025550001", meridian, {
        "extension": "101", "sip_password": "secret-101", "voicemail_enabled": True,
        "voicemail_pin": "4321", "voicemail_email": "desk101@example.com", "recording_enabled": True,
    })
    store.add_extension_to_number("+13025550002", meridian, {"extension": "102", "sip_password": "secret-102"})
    store.add_extension_to_number("+13025550098", meridian, {
        "extension": "101", "sip_password": "secret-101b", "voicemail_enabled": True, "voicemail_pin": "8765",
    })
    store.add_extension_to_number("+13025550098", meridian, {"extension": "105", "sip_password": "secret-105"})
    store.save_extension({"extension": "900", "sip_password": "secret-900"})
    store.save_sip_account({
        "label": "Reception", "sip_username": "reception", "sip_password": "device-secret",
        "server": "sip.example.com", "extension": first_101["key"], "phone_number": "+13025550001",
    }, meridian)
    store.set_settings({"default_extension": "101"})

    # Put the table back the way the older releases created it, and rewrite every
    # reference to the shapes those releases used. The previous release stored the
    # whole key in `extension`; the one before that stored bare digits, which is
    # still how a desk created before its number looked and how the operator's own
    # row looked.
    with store._connect() as db:  # noqa: SLF001 - building the fixture
        db.executescript("""
            UPDATE extensions SET extension = extension || '@' ||
                (SELECT number FROM phone_numbers WHERE id = extensions.phone_number_id)
                WHERE phone_number_id IS NOT NULL;
            UPDATE extensions SET extension = '105' WHERE extension LIKE '105@%';  -- never named by a number
            CREATE TABLE extensions_legacy (
                extension TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                sip_username TEXT NOT NULL, sip_password_enc TEXT NOT NULL,
                webrtc_enabled INTEGER NOT NULL DEFAULT 0, recording_enabled INTEGER NOT NULL DEFAULT 1,
                voicemail_enabled INTEGER NOT NULL DEFAULT 0, voicemail_pin_enc TEXT NOT NULL DEFAULT '',
                voicemail_email TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                owner_user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO extensions_legacy(
                extension,display_name,sip_username,sip_password_enc,webrtc_enabled,recording_enabled,
                voicemail_enabled,voicemail_pin_enc,voicemail_email,active,owner_user_id)
            SELECT extension,display_name,sip_username,sip_password_enc,webrtc_enabled,recording_enabled,
                voicemail_enabled,voicemail_pin_enc,voicemail_email,active,owner_user_id
            FROM extensions;
            DROP TABLE extensions;
            ALTER TABLE extensions_legacy RENAME TO extensions;
            UPDATE phone_numbers SET inbound_extension = '101' WHERE number='+13025550001';
            UPDATE phone_numbers SET inbound_extension = '102' WHERE number='+13025550002';
            UPDATE phone_numbers SET inbound_extension = '101' WHERE number='+13025550098';
            UPDATE customer_sip_accounts SET extension = '101';
            UPDATE admin_users SET extension = '101' WHERE username='meridian';
            DELETE FROM settings WHERE `key` IN ('extension_identity_v2_initialized','extensions_unassigned');
        """)
    # The fixture really is the old shape: no id, no phone_number_id on a row.
    with sqlite3.connect(str(path)) as raw:
        columns = {row[1] for row in raw.execute("PRAGMA table_info(extensions)").fetchall()}
        assert columns and "id" not in columns and "phone_number_id" not in columns
    return {"meridian": meridian}


def test_an_existing_install_migrates_without_losing_a_device(tmp_path):
    path = tmp_path / "settings.db"
    fixture = legacy_database(path)

    # Reopen: this is the upgrade, and it runs once.
    store = SettingsStore(str(path), "a" * 40)

    rows = {(row["digits"], row["number"]): row for row in store.list_extensions()}
    # Two rows carried the digits 101 - one on +1, one on +98 - and both are
    # still here, on their own number, with their own credentials. Nothing was
    # merged into a single "first 101".
    assert ("101", "+13025550001") in rows and ("101", "+13025550098") in rows
    assert len([key for key in rows if key[0] == "101"]) == 2
    # The secret each row stored travelled across the rebuild intact - the one
    # that had a device account keeps that account's secret as its live one.
    assert store.get_extension_password("101@+13025550001") == "secret-101"
    assert store.get_extension_password("101@+13025550098") == "secret-101b"
    assert store.get_extension_password("102@+13025550002") == "secret-102"
    # 105's number never named it: it belongs to the account's main line, which
    # is where provisioning would have put it.
    assert ("105", "+13025550001") in rows
    assert store.get_extension_password("105@+13025550001") == "secret-105"

    # Voicemail and recording travelled with the row, and the mailboxes are the
    # number-scoped ones - two 101s, two boxes.
    assert store.reveal_extension_credentials("101@+13025550098")["mailbox"] == "101-13025550098"
    assert rows[("101", "+13025550001")]["recording_enabled"] == 1
    assert store.get_voicemail_pin("101@+13025550001") == "4321"
    assert store.get_voicemail_pin("101@+13025550098") == "8765"

    # The number's inbound link is the key of the row that answers it.
    numbers = {row["number"]: row for row in store.list_numbers(fixture["meridian"])}
    assert numbers["+13025550001"]["inbound_extension"] == "101@+13025550001"
    assert numbers["+13025550098"]["inbound_extension"] == "101@+13025550098"
    assert numbers["+13025550002"]["inbound_extension"] == "102@+13025550002"

    # The device account kept its line: it is keyed to this very extension.
    account = next(
        row for row in store.list_sip_accounts(fixture["meridian"])
        if row["extension"] == "101@+13025550001"
    )
    assert account["phone_number"] == "+13025550001"
    assert account["label"] == "Reception"
    assert store.reveal_extension_credentials("101@+13025550001")["registration"] == "device"
    assert store.reveal_extension_credentials("101@+13025550001")["sip_password"] == "device-secret"
    # ...and the other line's 101 is a different device with its own secret.
    assert store.reveal_extension_credentials("101@+13025550098")["registration"] == "extension"

    # The operator's own 900 has no customer and no line: it is published as
    # unassigned rather than being merged onto a customer's number.
    assert all(row["digits"] != "900" for row in store.list_extensions(fixture["meridian"]))
    assert [row["extension"] for row in store.list_extensions() if row["digits"] == "900"] == ["900"]
    # The migration ran once: a second open changes nothing.
    before = sorted((row["digits"], row["number"]) for row in store.list_extensions())
    reopened = SettingsStore(str(path), "a" * 40)
    assert sorted((row["digits"], row["number"]) for row in reopened.list_extensions()) == before

    # What comes out the other side is what the dial plan and the phone register
    # against: a context per number, an endpoint per extension, two mailboxes.
    sync = TelephonyConfigSync(store, None, str(tmp_path / "pjsip.dynamic.conf"))
    pjsip = sync.render_pjsip()
    assert "\n[101-13025550001]\ntype=endpoint" in pjsip
    assert "\n[101-13025550098]\ntype=endpoint" in pjsip
    voicemail = sync.render_voicemail()
    assert "101-13025550001 =>" in voicemail and "101-13025550098 =>" in voicemail
    dialplan = sync.render_dialplan()
    context = dialplan.split(f"\n[{TelephonyConfigSync.number_context('+13025550098')}]\n")[1].split("\n\n")[0]
    own_101 = context.split("exten => 101,1")[1].split("exten => ")[0]
    assert "Dial(PJSIP/101-13025550098,30)" in own_101
    assert "VoiceMail(101-13025550098@engineerip,u)" in own_101
    assert "101-13025550001" not in own_101        # the other line's 101, two desks apart
