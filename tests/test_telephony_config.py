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
    # the SIP username must exist as an AOR alongside the extension number -
    # whatever the WebRTC switch says: a hardware phone or softphone registers
    # with the credential on its sheet and nothing here may take that away.
    assert f"[{username}]\ntype=aor" in text
    assert f"aors=101,{username}" in text
    # The WebRTC alias endpoint - the “[username] type=endpoint” a browser signs in
    # with - is rendered only for an extension whose WebRTC switch is on, so
    # plain SIP is unaffected by it either way.
    assert f"[{username}]\ntype=endpoint" not in text
    assert text.count("auth=auth-101") == 1  # the canonical endpoint only
    store.save_extension({"extension": "101", "webrtc_enabled": True})
    with_webrtc = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf")).render_pjsip()
    assert f"[{username}]\ntype=endpoint" in with_webrtc
    assert with_webrtc.count("auth=auth-101") == 2  # canonical + WebRTC alias
    assert f"aors=101,{username}" in with_webrtc
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
    # Media address on all three transports: the browser's ICE candidates come
    # from the WSS transport. Signalling stays off it - that leg already came in
    # through the reverse proxy.
    assert text.count("external_media_address=sip.example.com") == 3
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
    # The mailbox is the digits plus the number they belong to, so two 101s on
    # two lines have two separate boxes - never the digits alone.
    assert "101-13025550101 => 4321,Sales,,,attach=no|delete=no" in voicemail
    assert "VoiceMail(101-13025550101@engineerip,u)" in dialplan
    pjsip = sync.render_pjsip()
    assert "mailboxes=101-13025550101@engineerip" in pjsip
    assert "send_pai=yes" in pjsip
    assert "VoiceMailMain(@engineerip)" in dialplan
    assert "exten => 13025550101,1" in dialplan
    assert "exten => +13025550101,1" in dialplan
    assert "rings extension 101" in dialplan
    # The endpoint answers in the context of the number it belongs to.
    assert f"[{TelephonyConfigSync.number_context('+13025550101')}]" in dialplan
    assert "[101-13025550101]\ntype=endpoint" in pjsip


def test_the_two_101s_present_their_own_number_and_keep_their_own_mailbox(tmp_path: Path):
    """Duplicate 101s are two devices: each dials out as its own line and takes
    its own voicemail. Neither the caller ID nor the mailbox is shared, and 104
    stays on the line that was given one."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_provider({
        "name": "Carrier", "server": "sip.example.com", "username": "user", "password": "secret",
        "allowed_ips": "198.51.100.10/32", "codecs": "ulaw,alaw",
    })
    store.save_user({"username": "meridian", "password": "customer-password-1", "role": "user",
                     "email": "m@example.com", "company_name": "Meridian"})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    line_a, line_b = "+13025550098", "+13025550067"
    for number in (line_a, line_b):
        store.save_number({"number": number, "provider": "Carrier", "owner_user_id": meridian, "active": True})
    store.add_extension_to_number(line_a, meridian, {"extension": "101", "voicemail_enabled": True, "voicemail_pin": "4321"})
    store.add_extension_to_number(line_a, meridian, {"extension": "104"})
    store.add_extension_to_number(line_b, meridian, {"extension": "101", "voicemail_enabled": True, "voicemail_pin": "8765"})
    store.save_number({"number": line_a, "provider": "Carrier", "owner_user_id": meridian, "active": True,
                       "inbound_extension": f"101@{line_a}"})
    store.save_number({"number": line_b, "provider": "Carrier", "owner_user_id": meridian, "active": True,
                       "inbound_extension": f"101@{line_b}"})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))

    pjsip = sync.render_pjsip()
    endpoint_a = pjsip.split("[101-13025550098]\ntype=endpoint")[1].split("\n\n")[0]
    endpoint_b = pjsip.split("[101-13025550067]\ntype=endpoint")[1].split("\n\n")[0]
    # The caller ID travels with the line the device answers on.
    assert "set_var=OUTBOUND_CID=+13025550098" in endpoint_a
    assert "set_var=OUTBOUND_CID=+13025550067" in endpoint_b
    assert "13025550067" not in endpoint_a and "13025550098" not in endpoint_b

    # Two separate voicemail boxes, and each line's own extension reaches its own.
    voicemail = sync.render_voicemail()
    assert "101-13025550098 => 4321," in voicemail
    assert "101-13025550067 => 8765," in voicemail
    dialplan = sync.render_dialplan()
    context_a = dialplan.split(f"\n[{TelephonyConfigSync.number_context(line_a)}]\n")[1].split("\n\n")[0]
    context_b = dialplan.split(f"\n[{TelephonyConfigSync.number_context(line_b)}]\n")[1].split("\n\n")[0]
    own_a = context_a.split("exten => 101,1")[1].split("exten => ")[0]
    own_b = context_b.split("exten => 101,1")[1].split("exten => ")[0]
    assert "Dial(PJSIP/101-13025550098,30)" in own_a
    assert "VoiceMail(101-13025550098@engineerip,u)" in own_a
    assert "Dial(PJSIP/101-13025550067,30)" in own_b
    assert "VoiceMail(101-13025550067@engineerip,u)" in own_b
    assert "VoiceMailMain(@engineerip)" in context_a and "VoiceMailMain(@engineerip)" in context_b

    # The other line's full number still routes internally - the same customer
    # owns it - straight to that number's own 101 and its own box, not the trunk.
    local_b = context_a.split("exten => +13025550067,1")[1].split("exten => ")[0]
    assert "Dial(PJSIP/101-13025550067,30)" in local_b
    assert "OUTBOUND_TRUNK" not in local_b

    # 104 exists on the first line only: the second line's context has no such
    # extension and plays NOT IN SERVICE instead of borrowing it.
    assert "exten => 104,1" in context_a
    assert "Dial(PJSIP/104-13025550098,30)" in context_a
    assert "exten => 104,1" not in context_b
    # The first line's 104 is reached from the second only by full number - and
    # only because both lines are one customer's; bare 104 stays NOT IN SERVICE.
    assert "exten => +13025550098*104,1" in context_b
    assert "exten => 104,1" not in context_b


def test_both_101s_register_with_their_own_globally_unique_identity(tmp_path: Path):
    """Two lines' 101s are two SIP identities, and neither is the digits.

    A phone signs in with its own username, so the registrar matches a name that
    exists once in the whole platform: `MERIDIAN_101_<number>` for each line. The
    digits alone are what a person dials - they are never a registration."""
    store = SettingsStore(str(tmp_path / "settings.db"), "b" * 40)
    store.save_provider({
        "name": "Carrier", "server": "sip.example.com", "username": "user", "password": "secret",
        "allowed_ips": "198.51.100.10/32", "codecs": "ulaw,alaw",
    })
    store.save_user({"username": "meridian", "password": "customer-password-1", "role": "user",
                     "email": "m@example.com", "company_name": "Meridian"})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    for number in ("+13025550098", "+13025550067"):
        store.save_number({"number": number, "provider": "Carrier", "owner_user_id": meridian, "active": True})
        store.add_extension_to_number(number, meridian, {"extension": "101"})
    rows = store.list_extensions(meridian)
    assert {row["key"] for row in rows} == {"101@+13025550098", "101@+13025550067"}
    usernames = {str(row["sip_username"]) for row in rows}
    # Globally unique, and derived from the account, the digits and the line.
    assert usernames == {"MERIDIAN_101_13025550098", "MERIDIAN_101_13025550067"}
    assert not any(name.isdigit() for name in usernames)

    pjsip = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf")).render_pjsip()
    for row in rows:
        username = str(row["sip_username"])
        digits = "13025550098" if row["key"].endswith("98") else "13025550067"
        # The registrar finds the name: an AOR carries it, and the auth that
        # guards the endpoint names it - what a phone is configured with.
        assert f"[{username}]\ntype=aor" in pjsip
        assert f"[auth-101-{digits}]\ntype=auth" in pjsip
        assert f"username={username}" in pjsip
        # The endpoint a call rings is the digits plus the line, never the digits
        # alone and never the SIP username.
        assert f"[101-{digits}]\ntype=endpoint" in pjsip
        assert f"[auth-101-{digits}]\ntype=auth" in pjsip
    assert "\n[101]\ntype=endpoint" not in pjsip
    assert "PJSIP/101," not in pjsip


def test_webrtc_off_leaves_normal_sip_alone_and_on_adds_the_browser(tmp_path: Path):
    """Checking the WebRTC box adds the browser endpoint; it never removes the
    one a hardware phone or softphone registers on."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_provider({
        "name": "Carrier", "server": "sip.example.com", "username": "user", "password": "secret",
        "allowed_ips": "198.51.100.10/32", "codecs": "ulaw,alaw",
    })
    store.save_user({"username": "meridian", "password": "customer-password-1", "role": "user",
                     "email": "m@example.com", "company_name": "Meridian"})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    number = "+13025550098"
    store.save_number({"number": number, "provider": "Carrier", "owner_user_id": meridian, "active": True})
    row = store.add_extension_to_number(number, meridian, {"extension": "101"})
    store.save_number({"number": number, "provider": "Carrier", "owner_user_id": meridian, "active": True,
                       "inbound_extension": row["key"]})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    username = str(row["sip_username"])

    # WebRTC unchecked: the extension registers and is dialled as itself, and no
    # browser endpoint exists at all.
    off = sync.render_pjsip()
    assert "\n[101-13025550098]\ntype=endpoint" in off
    assert f"\n[{username}]\ntype=endpoint" not in off
    assert off.count("webrtc=yes") == 0

    # Checking it registers the browser identity as a second endpoint over WSS
    # and leaves the SIP one exactly where it was.
    store.save_extension({"extension": row["key"], "webrtc_enabled": True}, meridian)
    on = sync.render_pjsip()
    sip_before = off.split("[101-13025550098]\ntype=endpoint")[1].split("\n\n")[0]
    sip_after = on.split("[101-13025550098]\ntype=endpoint")[1].split("\n\n")[0]
    alias = on.split(f"[{username}]\ntype=endpoint")[1].split("\n\n")[0]
    assert sip_before == sip_after                       # normal SIP untouched
    assert "transport=transport-wss" in alias and "webrtc=yes" in alias
    assert "transport=transport-udp" in sip_after        # the phone still registers here


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
    """Both endpoints of an extension have to be ready for a browser.

    The browser signs in with the extension's generated SIP username, so the
    endpoint named after that username carries the WSS transport, Asterisk's
    WebRTC switch and mandatory encryption. Calls are placed *towards* an
    extension through the canonical endpoint - which is therefore
    WebRTC-capable too, with media_encryption_optimistic so a hardware phone or
    softphone that only speaks plain RTP still connects. Without that, a call to
    a phone signed in on the browser rings and then has no audio, because a
    browser cannot accept an unencrypted media offer."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "sip_username": "101", "sip_password": "secret", "webrtc_enabled": True})
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
    assert f"allow={TelephonyConfigSync.INTERNAL_CODECS}" in alias
    assert "transport=transport-udp" not in alias
    canonical = text.split("[101]\ntype=endpoint")[1].split("\n\n")[0]
    assert "transport=transport-udp" in canonical
    # The endpoint the dial plan rings by default stays plain RTP: a WebRTC
    # offer is refused by a hardware phone, so the two media modes live on two
    # endpoints and an extension picks between them (see the dial-plan test).
    assert "webrtc=yes" not in canonical
    assert "media_encryption_optimistic" not in canonical

    # The rendered transports file is what Asterisk actually includes.
    sync.apply()
    assert "[transport-wss]" in (tmp_path / "pjsip.transports.conf").read_text()

def test_internal_calls_negotiate_hd_voice_first(tmp_path: Path):
    """G.722 is wideband and built into Asterisk, so every device the platform
    provisions offers it first; PCMU/PCMA stay behind it so a device - or a
    carrier - that cannot do wideband still gets a call instead of a failure."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "sip_username": "101", "sip_password": "secret"})
    store.save_sip_account({
        "label": "Reception", "sip_username": "reception", "sip_password": "secret2",
        "server": "sip.example.com", "port": 5060, "transport": "udp",
    }, 1)
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    text = sync.render_pjsip()

    assert TelephonyConfigSync.INTERNAL_CODECS == "g722,ulaw,alaw"
    # disallow=all first, so nothing outside the policy can be negotiated.
    # The extension, and the device account - whose two sections (its hashed
    # name and its SIP username) are the same registration contract.
    assert text.count("disallow=all\nallow=g722,ulaw,alaw") == 3
    assert "allow=ulaw,alaw" not in text
    # Every codec on the list has to be one Asterisk can actually translate.
    assert TelephonyConfigSync._codecs(TelephonyConfigSync.INTERNAL_CODECS) == TelephonyConfigSync.INTERNAL_CODECS


def test_browser_media_advertises_the_public_address(tmp_path: Path):
    """A WebRTC call's ICE candidates come from the WSS transport, so it needs
    the admin panel's service address just like the UDP/TCP transports - a
    browser cannot send audio to the container's private address."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "sip_username": "101", "sip_password": "secret"})
    store.set_settings({"service_host": "sip.engineerip.com"})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))

    wss = sync.render_transports().split("[transport-wss]")[1]
    assert "external_media_address=sip.engineerip.com" in wss
    assert "local_net=10.0.0.0/8" in wss
    # Signalling continues on the WebSocket the proxy already opened.
    assert "external_signaling_address" not in wss


def test_every_number_has_its_own_extension_set(tmp_path: Path):
    """A three-digit extension is resolved only within the current phone number.

    Meridian holds two numbers and each starts its own set at 101, with 104 only
    on the first one. The rendered dial plan gives every number its own context:
    dialling 104 on +13025550098 rings that line's 104, the same digits on
    +13025550067 are NOT IN SERVICE, and another organisation's 101 is not in
    the context at all.
    """
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_provider({
        "name": "Carrier", "server": "sip.example.com", "port": 5060, "username": "user",
        "password": "secret", "transport": "udp", "codecs": "ulaw,alaw", "allowed_ips": "198.51.100.10/32",
    })
    store.save_user({"username": "meridian", "password": "customer-password-1", "role": "user",
                     "email": "m@example.com", "company_name": "Meridian"})
    store.save_user({"username": "northwind", "password": "customer-password-1", "role": "user",
                     "email": "n@example.com", "company_name": "Northwind"})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    northwind = next(row["id"] for row in store.list_users() if row["username"] == "northwind")
    line_a, line_b, north_line = "+13025550098", "+13025550067", "+13025550011"
    for number, owner in ((line_a, meridian), (line_b, meridian), (north_line, northwind)):
        store.save_number({"number": number, "provider": "Carrier", "owner_user_id": owner, "active": True})
    for extension in ("101", "104"):
        store.add_extension_to_number(line_a, meridian, {"extension": extension})
    for extension in ("101", "105"):
        store.add_extension_to_number(line_b, meridian, {"extension": extension})
    store.add_extension_to_number(north_line, northwind, {"extension": "101"})
    for number, owner, link in ((line_a, meridian, "101"), (line_b, meridian, "101"), (north_line, northwind, "101")):
        store.save_number({
            "number": number, "provider": "Carrier", "owner_user_id": owner, "active": True,
            "inbound_extension": f"{link}@{number}",
        })
    store.save_extension({"extension": "900", "active": True})                 # the platform's own line
    store.set_settings({"service_host": "sip.engineerip.com"})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))

    dialplan = sync.render_dialplan()
    context_a = dialplan.split(f"\n[{TelephonyConfigSync.number_context(line_a)}]\n")[1].split("\n\n")[0]
    context_b = dialplan.split(f"\n[{TelephonyConfigSync.number_context(line_b)}]\n")[1].split("\n\n")[0]
    north_context = dialplan.split(f"\n[{TelephonyConfigSync.number_context(north_line)}]\n")[1].split("\n\n")[0]
    platform_context = dialplan.split("\n[from-internal]\n")[1].split("\n\n")[0]

    # This line's own set, dialled on this line's own devices.
    assert f"exten => 101,1,NoOp(EngineerIP extension 101 on {line_a})" in context_a
    assert f"Dial(PJSIP/101-13025550098,30)" in context_a
    assert "exten => 104,1" in context_a
    assert "exten => 105,1" not in context_a        # 105 is the other line's desk
    assert "Dial(PJSIP/105-13025550067,30)" in context_b
    # The same digits on the other number are a different device - and a number
    # that has no 104 plays NOT IN SERVICE rather than borrowing one.
    assert "exten => 104,1" not in context_b
    assert "exten => _XXX,1" in context_b and "Playback(ss-noservice)" in context_b
    # 104 on the first line is reachable from the second only by its full number,
    # which is allowed because both lines belong to one customer - bare 104 is not.
    assert "exten => +13025550098*104,1" in context_b
    # Northwind's line is a full number from here: it rings northwind's own
    # inbound destination on the platform, and its other desks are refused.
    assert "exten => +13025550011,1" in context_a and "exten => _+13025550011*X.,1" in context_a
    assert "exten => +13025550011*101" not in context_a
    assert f"Dial(PJSIP/101-13025550011,30)" in north_context
    # Northwind reaches meridian's line by its full number, never its desks.
    assert "exten => +13025550098,1" in north_context
    assert "+13025550098*104" not in north_context and "+13025550098*101,1" not in north_context

    # The operator's own devices reach the platform's line, not a customer's.
    assert "exten => 900,1" in platform_context
    assert "exten => 101,1" not in platform_context

    # Voicemail login and outbound dialling keep working in every context.
    for context in (context_a, context_b, north_context, platform_context):
        assert "VoiceMailMain(@engineerip)" in context
        assert "exten => _XXXX.,1,NoOp(Outbound" in context
        assert "Dial(PJSIP/${EXTEN}@${OUTBOUND_TRUNK},60)" in context


def test_endpoints_answer_in_the_context_of_the_number_that_owns_them(tmp_path: Path):
    """The endpoint's context is written where the device registers.

    A device of +13022661626 only finds that line's extension set, a device of
    the other customer's line finds only its own, and the platform's own device
    stays in the platform context - it has no line, so it has no extension set
    to dial into.
    """
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_user({"username": "meridian", "password": "customer-password-1", "role": "user",
                     "email": "m@example.com", "company_name": "Meridian"})
    store.save_user({"username": "northwind", "password": "customer-password-1", "role": "user",
                     "email": "n@example.com", "company_name": "Northwind"})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    northwind = next(row["id"] for row in store.list_users() if row["username"] == "northwind")
    store.save_number({"number": "+13022661626", "owner_user_id": meridian, "active": True})
    store.save_number({"number": "+13022669999", "owner_user_id": northwind, "active": True})
    store.add_extension_to_number("+13022661626", meridian, {"extension": "101"})
    store.add_extension_to_number("+13022669999", northwind, {"extension": "117"})
    store.save_extension({"extension": "900", "active": True})            # the platform's own line
    # A device account that is not linked to an extension answers in the
    # platform's context: with no line it cannot name an extension set.
    store.save_sip_account({"label": "Reception", "sip_username": "meridian-reception-device",
                            "sip_password": "secret", "server": "sip.example.com",
                            "extension": "", "active": True}, meridian)
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    text = sync.render_pjsip()

    def endpoint(name: str) -> str:
        return text.split(f"[{name}]\ntype=endpoint")[1].split("\n\n")[0]

    assert "context=from-number-13022661626" in endpoint("101-13022661626")
    assert "context=from-number-13022669999" in endpoint("117-13022669999")
    # The platform's own extension keeps the platform context, where no
    # customer's extension is reachable.
    assert "context=from-internal\n" in endpoint("900")
    assert store.get_number_owner("+13022661626") == meridian


def test_a_did_only_rings_an_extension_of_its_own_number(tmp_path: Path):
    """A number routes to its own line's extensions - never to another line.

    A DID with a stale link that names another organisation's digits rings the
    extension of its own number instead, and a number whose account holds no
    extension at all rings nobody: it never borrows another organisation's
    phone, not even through the platform-wide fallback that points at one.
    """
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_user({"username": "meridian", "password": "customer-password-1", "role": "user",
                     "email": "m@example.com", "company_name": "Meridian"})
    store.save_user({"username": "northwind", "password": "customer-password-1", "role": "user",
                     "email": "n@example.com", "company_name": "Northwind"})
    store.save_user({"username": "bluewave", "password": "customer-password-1", "role": "user",
                     "email": "b@example.com", "company_name": "Bluewave"})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    northwind = next(row["id"] for row in store.list_users() if row["username"] == "northwind")
    bluewave = next(row["id"] for row in store.list_users() if row["username"] == "bluewave")
    store.save_number({"number": "+13025550098", "owner_user_id": meridian, "active": True})
    store.save_number({"number": "+13025550011", "owner_user_id": northwind, "active": True})
    store.save_number({"number": "+13025550022", "owner_user_id": bluewave, "active": True})
    store.add_extension_to_number("+13025550098", meridian, {"extension": "101"})
    store.add_extension_to_number("+13025550011", northwind, {"extension": "117"})
    store.save_number({"number": "+13025550098", "owner_user_id": meridian, "active": True,
                       "inbound_extension": "101@+13025550098"})
    # A row written before ownership was enforced: northwind's DID pointing at
    # meridian's extension. It rings northwind's own line instead.
    with store._connect() as db:
        db.execute("UPDATE phone_numbers SET inbound_extension='101' WHERE number='+13025550011'")
    # ...and the operator's platform-wide fallback points at northwind too.
    store.set_settings({"inbound_fallback_extension": "117"})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    dialplan = sync.render_dialplan()

    assert "exten => 13025550098,1,NoOp(Inbound DID 13025550098 rings extension 101)" in dialplan
    stale = dialplan.split("exten => 13025550011,1,NoOp(Inbound DID")[1].split("\nexten")[0]
    assert "rings extension 117" in stale
    assert "Stasis(engineerip,inbound,13025550011,101@+13025550098)" not in dialplan

    # A number whose account has no reachable extension rings nobody: it never
    # borrows another organisation's phone, not even through the platform-wide
    # fallback that points at one.
    unreachable = dialplan.split("exten => 13025550022,1,NoOp(Inbound DID")[1].split("\nexten")[0]
    assert "has no reachable extension" in unreachable and "Playback(ss-noservice)" in unreachable
    assert "Stasis(engineerip,inbound,13025550022,117" not in dialplan


def test_a_customer_dials_its_own_numbers_internally(tmp_path: Path):
    """Dialling one of the account's own numbers reaches that number's extension.

    The field example, one customer holding two numbers: a phone on
    +13025550098 dials +13025550067 and reaches the extension that number is set
    to ring - the *other* line's own extension, which is exactly what a full
    number names. The call is looked up in the line's own context and never
    handed to the carrier trunk, which is what makes it reliable: the carrier
    may refuse to connect a number to itself, or not deliver it at all.
    """
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_provider({
        "name": "IPComms", "server": "sip.example.com", "port": 5060, "username": "trunk",
        "password": "provider-secret", "transport": "udp", "codecs": "ulaw,alaw",
        "allowed_ips": "203.0.113.10/32",
    })
    for username, company in (("meridian", "Meridian"), ("northwind", "Northwind")):
        store.save_user({"username": username, "password": "customer-password-1", "role": "user",
                         "email": f"{username[0]}@example.com", "company_name": company})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    northwind = next(row["id"] for row in store.list_users() if row["username"] == "northwind")
    for number, owner in (("+13025550098", meridian), ("+13025550067", meridian), ("+13025550011", northwind)):
        store.save_number({"number": number, "provider": "IPComms", "owner_user_id": owner, "active": True})
    store.add_extension_to_number("+13025550098", meridian, {"extension": "105"})
    store.add_extension_to_number("+13025550067", meridian, {"extension": "117"})
    store.add_extension_to_number("+13025550011", northwind, {"extension": "201"})
    for number, owner, link in (("+13025550098", meridian, "105"), ("+13025550067", meridian, "117"),
                                ("+13025550011", northwind, "201")):
        store.save_number({"number": number, "provider": "IPComms", "owner_user_id": owner,
                           "active": True, "inbound_extension": f"{link}@{number}"})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    dialplan = sync.render_dialplan()
    context_a = dialplan.split(f"\n[{TelephonyConfigSync.number_context('+13025550098')}]\n")[1].split("\n\n")[0]
    context_b = dialplan.split(f"\n[{TelephonyConfigSync.number_context('+13025550067')}]\n")[1].split("\n\n")[0]
    north_context = dialplan.split(f"\n[{TelephonyConfigSync.number_context('+13025550011')}]\n")[1].split("\n\n")[0]

    # 105 dials the customer's other number and lands on that number's 117 ...
    route = context_a.split("exten => 13025550067,1,")[1].split("\nexten")[0]
    assert "local number 13025550067 rings extension 117" in route
    assert "Dial(PJSIP/117-13025550067,30)" in route
    # ... and the carrier trunk is nowhere in that call.
    assert "OUTBOUND_TRUNK" not in route
    # The + form is what a phone with a country-code dial plan sends.
    assert "exten => +13025550067,1,NoOp(EngineerIP local number +13025550067 rings extension 117)" in context_a
    # Its own number is dialable the same way - and it is its own 105, not the
    # other line's 117.
    assert "local number 13025550098 rings extension 105" in context_b
    assert "Dial(PJSIP/105-13025550098,30)" in context_b

    # Another customer's number is dialled by its full number and rings its own
    # inbound destination on the platform - it never leaves through the trunk.
    assert "exten => 13025550011,1,NoOp(EngineerIP local number 13025550011 rings extension 201)" in context_a
    assert "Dial(PJSIP/201-13025550011,30)" in context_a
    # ...but that customer's other desks are NOT IN SERVICE from here.
    assert "exten => _13025550011*X.,1" in context_a
    assert "exten => 13025550011*201" not in context_a
    # And Northwind reaches meridian's two lines by full number, at their inbound destinations.
    assert "local number 13025550098 rings extension 105" in north_context
    assert "local number 13025550067 rings extension 117" in north_context
    assert "exten => 13025550098*105" not in north_context


def test_a_number_that_cannot_ring_one_of_its_own_extensions_is_external(tmp_path: Path):
    """Only a number that really rings an extension of its own line is local.

    An inactive number, and a number whose account has no extension on it,
    fall through to ordinary outbound dialling instead: a local shortcut must
    never become a way to reach a stranger's desk.
    """
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_provider({
        "name": "IPComms", "server": "sip.example.com", "port": 5060, "username": "trunk",
        "password": "provider-secret", "transport": "udp", "codecs": "ulaw,alaw",
        "allowed_ips": "203.0.113.10/32",
    })
    for username in ("meridian", "northwind"):
        store.save_user({"username": username, "password": "customer-password-1", "role": "user",
                         "email": f"{username[0]}@example.com", "company_name": username.title()})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    northwind = next(row["id"] for row in store.list_users() if row["username"] == "northwind")
    store.save_number({"number": "+13025550067", "provider": "IPComms", "owner_user_id": meridian, "active": True})
    store.add_extension_to_number("+13025550067", meridian, {"extension": "105"})
    store.save_number({"number": "+13025550067", "provider": "IPComms", "owner_user_id": meridian,
                       "active": True, "inbound_extension": "105@+13025550067"})
    # Discontinued: an inbound call does not ring it any more either.
    store.save_number({"number": "+13025550022", "provider": "IPComms", "owner_user_id": meridian, "active": False})
    store.save_number({"number": "+13025550011", "provider": "IPComms", "owner_user_id": northwind, "active": True})
    store.add_extension_to_number("+13025550011", northwind, {"extension": "201"})
    # A live number of meridian's whose own set is empty, with a stale link
    # naming northwind's extension: it can only ring what is on it - nothing.
    store.save_number({"number": "+13025550033", "provider": "IPComms", "owner_user_id": meridian, "active": True})
    with store._connect() as db:
        db.execute("UPDATE phone_numbers SET inbound_extension='201' WHERE number='+13025550033'")
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    dialplan = sync.render_dialplan()
    context = dialplan.split(f"\n[{TelephonyConfigSync.number_context('+13025550067')}]\n")[1].split("\n\n")[0]

    assert "local number 13025550067 rings extension 105" in context
    # A discontinued line is not a local number at all ...
    assert "exten => 13025550022,1" not in context
    # ... and a line this account owns but cannot ring stays inside the account:
    # it plays NOT IN SERVICE, and it never reaches the stale link's extension.
    assert "local number 13025550033 has no reachable extension" in context
    assert "exten => 201,1" not in context
    # Inbound, the stale link cannot reach that other extension either: the
    # number has nothing of its own to ring and says "not in service".
    inbound = dialplan.split("exten => 13025550033,1,NoOp(Inbound DID")[1].split("\nexten")[0]
    assert "has no reachable extension" in inbound and "Playback(ss-noservice)" in inbound
    assert "Stasis(engineerip,inbound,13025550033,201" not in dialplan


def test_dialling_an_extension_nobody_is_signed_in_as_says_so(tmp_path: Path):
    """A failed Dial leaves silence behind, which reads as "calling is broken".

    An extension with no registered phone now answers with "not in service"
    instead; an extension with voicemail still gets its voicemail, which is the
    better answer when it has one.
    """
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "active": True})
    store.save_extension({"extension": "102", "active": True, "voicemail_enabled": True, "voicemail_pin": "1234"})
    dialplan = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf")).render_dialplan()

    reachable = dialplan.split("exten => 101,1,")[1].split("\nexten")[0]
    assert "Dial(PJSIP/101,30)" in reachable
    assert 'ExecIf($["${DIALSTATUS}" = "CHANUNAVAIL"]?Playback(ss-noservice))' in reachable
    assert "VoiceMail(101@engineerip,u)" not in reachable

    voicemail = dialplan.split("exten => 102,1,")[1].split("\nexten")[0]
    assert "VoiceMail(102@engineerip,u)" in voicemail
    assert "CHANUNAVAIL" not in voicemail


def test_an_extension_that_answers_in_the_browser_is_dialled_on_its_webRTC_endpoint(tmp_path: Path):
    """Media is decided by the endpoint a call is placed towards.

    A browser cannot answer the plain endpoint's RTP offer (no audio at all),
    and a hardware phone refuses the WebRTC endpoint's DTLS-SRTP offer. So the
    extension an operator marked as answering in the browser is dialled on its
    WebRTC endpoint - and every other extension keeps the plain one, which is
    what the credentials a customer is given are written for.
    """
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_extension({"extension": "101", "sip_password": "secret", "webrtc_enabled": True, "active": True})
    store.save_extension({"extension": "102", "sip_password": "secret", "active": True})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    # The platform mints the SIP username an extension signs in with; that name
    # is the WebRTC endpoint, so the test reads it back rather than assuming it.
    web_username = next(row["sip_username"] for row in store.list_extensions() if row["extension"] == "101")
    desk_username = next(row["sip_username"] for row in store.list_extensions() if row["extension"] == "102")
    assert web_username != desk_username

    dialplan = sync.render_dialplan()
    browser = dialplan.split("exten => 101,1,")[1].split("\nexten")[0]
    assert f"Dial(PJSIP/{web_username},30)" in browser
    assert "Dial(PJSIP/101,30)" not in browser
    desk = dialplan.split("exten => 102,1,")[1].split("\nexten")[0]
    assert "Dial(PJSIP/102,30)" in desk
    assert f"PJSIP/{desk_username}" not in desk

    # The WebRTC settings really are on the endpoint that is now dialled.
    endpoint = sync.render_pjsip().split(f"[{web_username}]\ntype=endpoint")[1].split("\n\n")[0]
    assert "transport=transport-wss" in endpoint
    assert "webrtc=yes" in endpoint


def test_a_full_number_of_any_platform_line_is_dialled_inside_the_platform(tmp_path: Path):
    """Typing a number reaches it - of this customer or of another - never the carrier.

    Same setup as the per-number test. From meridian's +13025550098:
      * +13025550067 (meridian's own line) rings that line's 101, and its 105 by
        `+13025550067*105`;
      * +13025550011 (northwind's line) rings northwind's 101 - its inbound
        destination - but its 101 by `+13025550011*101` is NOT IN SERVICE: one
        customer's extensions are not dialable from another's line.
    The carrier catch-all only ever sees numbers the platform does not own.
    """
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_provider({
        "name": "Carrier", "server": "sip.example.com", "port": 5060, "username": "user",
        "password": "secret", "transport": "udp", "codecs": "ulaw,alaw", "allowed_ips": "198.51.100.10/32",
    })
    for username in ("meridian", "northwind"):
        store.save_user({"username": username, "password": "customer-password-1", "role": "user",
                         "email": f"{username}@example.com", "company_name": username.title()})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    northwind = next(row["id"] for row in store.list_users() if row["username"] == "northwind")
    line_a, line_b, north_line = "+13025550098", "+13025550067", "+13025550011"
    for number, owner in ((line_a, meridian), (line_b, meridian), (north_line, northwind)):
        store.save_number({"number": number, "provider": "Carrier", "owner_user_id": owner, "active": True})
    store.add_extension_to_number(line_a, meridian, {"extension": "101"})
    store.add_extension_to_number(line_b, meridian, {"extension": "101"})
    store.add_extension_to_number(line_b, meridian, {"extension": "105"})
    store.add_extension_to_number(north_line, northwind, {"extension": "101"})
    for number, owner in ((line_a, meridian), (line_b, meridian), (north_line, northwind)):
        store.save_number({"number": number, "provider": "Carrier", "owner_user_id": owner, "active": True,
                           "inbound_extension": f"101@{number}"})
    store.set_settings({"service_host": "sip.engineerip.com"})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))

    dialplan = sync.render_dialplan()
    context_a = dialplan.split(f"\n[{TelephonyConfigSync.number_context(line_a)}]\n")[1].split("\n\n")[0]
    north_context = dialplan.split(f"\n[{TelephonyConfigSync.number_context(north_line)}]\n")[1].split("\n\n")[0]

    # Meridian's own second line: its inbound destination, and its 105 by full number.
    assert "exten => +13025550067,1,NoOp(EngineerIP local number +13025550067 rings extension 101)" in context_a
    assert "exten => +13025550067*105,1" in context_a
    assert "Dial(PJSIP/105-13025550067,30)" in context_a

    # Northwind's line: rings its inbound destination, never its other extensions.
    assert "exten => +13025550011,1,NoOp(EngineerIP local number +13025550011 rings extension 101)" in context_a
    assert "Dial(PJSIP/101-13025550011,30)" in context_a
    assert "exten => _+13025550011*X.,1" in context_a
    assert "exten => +13025550011*101" not in context_a
    assert "exten => +13025550011*101,1" in north_context          # northwind dials its own 101 by number

    # Meridian cannot reach northwind's extensions by full number; northwind cannot reach meridian's.
    assert "exten => +13025550067*105" not in north_context
    assert "exten => +13025550098,1" in north_context and "exten => _+13025550098*X." in north_context

    # The carrier catch-all is only ever reached by numbers the platform does not own.
    assert context_a.index("exten => +13025550011,1") < context_a.index("exten => _+X.,1")
    assert "_+X." in context_a
