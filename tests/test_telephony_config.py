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
    assert f"[{username}]\ntype=aor" in text
    assert f"aors=101,{username}" in text
    # PJSIP's username/auth_username identifiers match against the endpoint
    # NAME, so an alias endpoint named after the SIP username (sharing the
    # extension's auth and AORs) must exist or every REGISTER from the phone
    # is challenged with a dummy auth that can never succeed.
    assert f"[{username}]\ntype=endpoint" in text
    assert text.count("auth=auth-101") == 2  # canonical + alias endpoint
    # Challenge with plain MD5 digest only: offering SHA-256 (RFC 8760) makes
    # common softphones (Zoiper 5) silently abandon the challenge.
    assert "supported_algorithms" not in text
    assert "SHA-256" not in text
    # And the global identifier order must allow auth_username to run at all.
    bootstrap = (Path(__file__).resolve().parents[1] / "asterisk" / "entrypoint.sh").read_text()
    assert "endpoint_identifier_order=ip,username,auth_username,anonymous" in bootstrap


def test_admin_chooses_the_sip_digest_algorithm(tmp_path: Path):
    """sip_auth_digest setting: md5 (default, maximum compatibility),
    sha256, or both - rendered into every account's auth section."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "sip_username": "101", "sip_password": "secret"})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))

    # Default: plain MD5 challenge, no supported_algorithms_uas at all.
    assert "supported_algorithms" not in sync.render_pjsip()

    store.set_settings({"sip_auth_digest": "both"})
    assert "supported_algorithms_uas=SHA-256,MD5" in sync.render_pjsip()

    store.set_settings({"sip_auth_digest": "sha256"})
    text = sync.render_pjsip()
    assert "supported_algorithms_uas=SHA-256\n" in text
    assert "SHA-256,MD5" not in text

    store.set_settings({"sip_auth_digest": "md5"})
    assert "supported_algorithms" not in sync.render_pjsip()

    import pytest
    with pytest.raises(ValueError):
        store.set_settings({"sip_auth_digest": "plaintext"})


def test_transports_use_the_admin_panel_service_address(tmp_path: Path, monkeypatch):
    """The admin panel's Service address is the single public-address source;
    .env's ASTERISK_EXTERNAL_ADDRESS is only a fallback until it is set."""
    monkeypatch.setenv("ASTERISK_EXTERNAL_ADDRESS", "198.51.100.99")
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))

    fallback = sync.render_transports()
    assert "external_signaling_address=198.51.100.99" in fallback

    store.set_settings({"service_host": "sip.example.com"})
    text = sync.render_transports()
    assert "[transport-udp]" in text and "[transport-tcp]" in text
    assert text.count("external_media_address=sip.example.com") == 2
    assert text.count("external_signaling_address=sip.example.com") == 2
    assert "external_signaling_address=198.51.100.99" not in text
    # Docker and LAN destinations are exempt from NAT rewriting.
    assert "local_net=172.16.0.0/12" in text

    sync.apply()
    rendered = (tmp_path / "pjsip.transports.conf").read_text()
    assert "external_signaling_address=sip.example.com" in rendered


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
    pjsip = sync.render_pjsip()
    trunk = sync._id("provider", "Carrier")

    # Outbound patterns exist for E.164 and plain digit dialling.
    assert "exten => _+X.,1,NoOp(Outbound" in dialplan
    assert "exten => _XXXX.,1,NoOp(Outbound" in dialplan
    assert "Dial(PJSIP/${EXTEN}@${OUTBOUND_TRUNK},60)" in dialplan

    # Extension 101's endpoints carry their own DID and carrier trunk as
    # channel variables, so any call it originates dials out as itself...
    assert "set_var=OUTBOUND_CID=+13025550101" in pjsip
    assert f"set_var=OUTBOUND_TRUNK={trunk}" in pjsip
    # ...while 102, which has no number, is blocked instead of spoofing one.
    assert "set_var=OUTBOUND_CID=\n" in pjsip
    assert "set_var=OUTBOUND_TRUNK=\n" in pjsip
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

def test_the_browser_softphone_gets_a_webRTC_endpoint(tmp_path: Path):
    """The browser signs in with the same prefixed SIP username as the desk
    phone, so the alias endpoint must carry the WSS transport and Asterisk's
    WebRTC switch; the canonical extension endpoint stays a plain UDP/TCP
    endpoint for Zoiper and hardware phones."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "sip_username": "101", "sip_password": "secret"})
    username = next(row["sip_username"] for row in store.list_extensions() if row["extension"] == "101")
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))

    transports = sync.render_transports()
    assert "[transport-wss]\ntype=transport\nprotocol=wss" in transports
    # WSS is carried by Asterisk's TLS HTTP listener (http.conf), so no port is
    # bound here and the UDP/TCP listeners stay untouched.
    assert transports.count("bind=0.0.0.0:5060") == 2

    text = sync.render_pjsip()
    alias = text.split(f"[{username}]\ntype=endpoint")[1].split("\n\n")[0]
    assert "transport=transport-wss" in alias
    assert "webrtc=yes" in alias
    assert "allow=ulaw,alaw" in alias
    assert "transport=transport-udp" not in alias
    canonical = text.split("[101]\ntype=endpoint")[1].split("\n\n")[0]
    assert "transport=transport-udp" in canonical
    assert "webrtc=yes" not in canonical

    # The rendered transports file is what Asterisk actually includes.
    sync.apply()
    assert "[transport-wss]" in (tmp_path / "pjsip.transports.conf").read_text()
