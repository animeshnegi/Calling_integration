from pathlib import Path

from app.admin import SettingsStore
from app.telephony_config import TelephonyConfigSync


class DummyAMI:
    def reload_pjsip(self):
        return {}

    def reload_dialplan(self):
        return {}

    def reload_voicemail(self):
        return {}


def test_provider_allowlist_renders_identify(tmp_path: Path):
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "sip_username": "101", "sip_password": "secret"})
    store.save_provider({
        "name": "TestProvider", "server": "sip.example.com", "port": 5060,
        "username": "user", "password": "password", "transport": "udp",
        "codecs": "ulaw,alaw", "allowed_ips": "198.51.100.10/32,198.51.100.0/24",
    })
    text = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf")).render_pjsip()
    assert "type=identify" in text
    assert "match=198.51.100.0/24" in text


def test_phones_can_register_with_their_prefixed_sip_username(tmp_path: Path):
    """A phone signs in as e.g. AUHFZH_101 while the endpoint is named 101.

    Asterisk identifies endpoints by matching the From-user against the
    endpoint name, so without auth_username identification and an AOR named
    after the SIP username every registration dies with InvalidAccountID.
    """
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "sip_username": "101", "sip_password": "secret"})
    username = next(row["sip_username"] for row in store.list_extensions() if row["extension"] == "101")
    assert username != "101" and username.endswith("_101")   # prefixed credential

    text = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf")).render_pjsip()
    # The endpoint can be found through the Authorization header username.
    assert "identify_by=username,auth_username" in text
    # The registrar resolves the To-user against the endpoint's AOR list, so
    # the SIP username must exist as an AOR alongside the extension number.
    assert f"[{username}]" in text
    assert f"aors=101,{username}" in text
    # And the global identifier order must allow auth_username to run at all.
    bootstrap = (Path(__file__).resolve().parents[1] / "asterisk" / "entrypoint.sh").read_text()
    assert "endpoint_identifier_order=ip,username,auth_username,anonymous" in bootstrap


def test_voicemail_mailbox_and_routes_are_rendered(tmp_path: Path):
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({
        "extension": "101", "display_name": "Sales", "sip_username": "101", "sip_password": "secret",
        "voicemail_enabled": True, "voicemail_pin": "4321",
    })
    store.save_provider({
        "name": "Carrier", "server": "sip.example.com", "username": "user", "password": "secret",
        "allowed_ips": "198.51.100.10/32", "codecs": "ulaw,alaw",
    })
    store.save_number({
        "number": "+13025550101", "provider": "Carrier", "inbound_extension": "101",
        "default_outbound": True,
    })
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    voicemail = sync.render_voicemail()
    dialplan = sync.render_dialplan()
    assert "101 => 4321,Sales,,,attach=no|delete=no" in voicemail
    assert "VoiceMail(101@engineerip,u)" in dialplan
    pjsip = sync.render_pjsip()
    assert "mailboxes=101@engineerip" in pjsip
    assert "send_pai=yes" in pjsip
    assert "VoiceMailMain(@engineerip)" in dialplan
    assert "exten => 13025550101,1" in dialplan
    assert "exten => +13025550101,1" in dialplan
    assert "owned by extension 101" in dialplan


def test_registered_phones_can_dial_out_through_their_own_number(tmp_path: Path):
    """A SIP device dialling an external number must reach the carrier trunk
    with its own DID as caller ID - before this, [from-internal] had no
    outbound pattern at all, so direct calls from phones always failed."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "sip_username": "101", "sip_password": "secret"})
    store.save_extension({"extension": "102", "sip_username": "102", "sip_password": "secret"})
    store.save_provider({
        "name": "Carrier", "server": "sip.example.com", "username": "user", "password": "secret",
        "allowed_ips": "198.51.100.10/32", "codecs": "ulaw,alaw",
    })
    store.save_number({
        "number": "+13025550101", "provider": "Carrier", "inbound_extension": "101",
        "default_outbound": True,
    })
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    dialplan = sync.render_dialplan()
    trunk = sync._id("provider", "Carrier")

    # Outbound patterns exist for E.164 and plain digit dialling.
    assert "exten => _+X.,1,NoOp(Outbound" in dialplan
    assert "exten => _XXXX.,1,NoOp(Outbound" in dialplan
    assert "Dial(PJSIP/${EXTEN}@${OUTBOUND_TRUNK},60)" in dialplan

    # Extension 101 dials out as its own number through its carrier...
    assert "[outbound-identity]" in dialplan
    assert "exten => 101,1,Set(OUTBOUND_CID=+13025550101)" in dialplan
    assert f"same => n,Set(OUTBOUND_TRUNK={trunk})" in dialplan
    # ...while 102, which has no number, is blocked instead of spoofing one.
    assert "exten => 102,1,Set(OUTBOUND_CID=)" in dialplan
    assert "Playback(ss-noservice)" in dialplan


def test_recording_defaults_to_allowed_globally_and_off_per_extension(tmp_path: Path):
    """The platform switch allows recording; every device still starts opted out,
    so nothing is recorded until a customer switches their own device on."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "sip_username": "101", "sip_password": "secret"})
    assert store.recording_platform_enabled() is True
    assert store.list_extensions()[0]["recording_enabled"] == 0
    store.set_settings({"recording_enabled": "false"})
    reopened = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    assert reopened.recording_platform_enabled() is False
