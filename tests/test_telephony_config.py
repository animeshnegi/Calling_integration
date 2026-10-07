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
    assert f"allow={TelephonyConfigSync.INTERNAL_CODECS}" in alias
    assert "transport=transport-udp" not in alias
    canonical = text.split("[101]\ntype=endpoint")[1].split("\n\n")[0]
    assert "transport=transport-udp" in canonical
    assert "webrtc=yes" not in canonical

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
    assert text.count("disallow=all\nallow=g722,ulaw,alaw") == 3   # extension + alias + device
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


def test_each_organisation_dials_its_own_extensions(tmp_path: Path):
    """Dial-by-extension is scoped to the account that owns the phone.

    A customer with two numbers keeps one extension set: every extension of the
    account is dialled by 3-digit number from any of its devices, whichever DID
    the extension belongs to. Another customer's extension is not in that dial
    plan at all, so a misdial cannot land on a different organisation's desk.
    """
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_user({"username": "meridian", "password": "customer-password-1", "role": "user",
                     "email": "m@example.com", "company_name": "Meridian"})
    store.save_user({"username": "northwind", "password": "customer-password-1", "role": "user",
                     "email": "n@example.com", "company_name": "Northwind"})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    northwind = next(row["id"] for row in store.list_users() if row["username"] == "northwind")
    store.save_extension({"extension": "101", "active": True}, meridian)
    store.save_extension({"extension": "102", "active": True}, meridian)
    store.save_extension({"extension": "105", "active": True}, meridian)
    store.save_extension({"extension": "117", "active": True}, northwind)
    store.save_extension({"extension": "118", "active": True}, northwind)
    store.save_extension({"extension": "900", "active": True})                 # the platform's own line
    store.set_settings({"service_host": "sip.engineerip.com"})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))

    dialplan = sync.render_dialplan()
    meridian_context = dialplan.split(f"\n[{TelephonyConfigSync.tenant_context(meridian)}]\n")[1].split("\n\n")[0]
    northwind_context = dialplan.split(f"\n[{TelephonyConfigSync.tenant_context(northwind)}]\n")[1].split("\n\n")[0]
    platform_context = dialplan.split("\n[from-internal]\n")[1].split("\n\n")[0]

    # Every extension of the account - from either of its numbers - is dialable.
    for extension in ("101", "102", "105", "900"):
        assert f"exten => {extension},1" in meridian_context
        assert f"Dial(PJSIP/{extension},30)" in meridian_context
    # ...and the other organisation's extensions are nowhere in it.
    assert "117" not in meridian_context and "118" not in meridian_context
    assert "101" not in northwind_context and "105" not in northwind_context
    assert "exten => 117,1" in northwind_context

    # The platform's own devices reach every extension, the way the operator's
    # line always has.
    for extension in ("101", "117", "900"):
        assert f"exten => {extension},1" in platform_context

    # Nothing falls through to the carrier: an unknown three-digit number is
    # answered with "not in service", which is what another organisation's
    # extension is from inside this context.
    assert "exten => _XXX,1" in meridian_context and "Playback(ss-noservice)" in meridian_context
    # Voicemail login and outbound dialling keep working in every context.
    for context in (meridian_context, northwind_context, platform_context):
        assert "VoiceMailMain(@engineerip)" in context
        assert "exten => _XXXX.,1,NoOp(Outbound" in context
        assert "Dial(PJSIP/${EXTEN}@${OUTBOUND_TRUNK},60)" in context


def test_endpoints_answer_in_the_context_of_the_account_that_owns_them(tmp_path: Path):
    """The endpoint's context is written where the device registers."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_user({"username": "meridian", "password": "customer-password-1", "role": "user",
                     "email": "m@example.com", "company_name": "Meridian"})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    store.save_extension({"extension": "101", "active": True}, meridian)
    store.save_extension({"extension": "102", "active": True}, meridian)
    store.save_extension({"extension": "900", "active": True})
    # A device account that is not linked to an extension still answers in its own
    # account's context instead of the platform's shared one.
    store.save_sip_account({"label": "Reception", "sip_username": "meridian-reception-device",
                            "sip_password": "secret", "server": "sip.example.com",
                            "extension": "", "active": True}, meridian)
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    text = sync.render_pjsip()
    context = TelephonyConfigSync.tenant_context(meridian)

    extension_101 = text.split("[101]\ntype=endpoint")[1].split("\n\n")[0]
    extension_102 = text.split("[102]\ntype=endpoint")[1].split("\n\n")[0]
    assert f"context={context}" in extension_101 and f"context={context}" in extension_102
    # The alias endpoint a browser signs in with carries the same context.
    username = next(row["sip_username"] for row in store.list_extensions() if row["extension"] == "101")
    alias = text.split(f"[{username}]\ntype=endpoint")[1].split("\n\n")[0]
    assert f"context={context}" in alias
    # The platform's own extension stays in the platform context.
    platform = text.split("[900]\ntype=endpoint")[1].split("\n\n")[0]
    assert "context=from-internal\n" in platform


def test_a_did_only_rings_an_extension_of_its_own_account(tmp_path: Path):
    """A number routes to its account's extensions: a DID must never ring another
    organisation's phone, not even through a stale inbound link."""
    store = SettingsStore(str(tmp_path / "settings.db"), "a" * 40)
    store.save_user({"username": "meridian", "password": "customer-password-1", "role": "user",
                     "email": "m@example.com", "company_name": "Meridian"})
    store.save_user({"username": "northwind", "password": "customer-password-1", "role": "user",
                     "email": "n@example.com", "company_name": "Northwind"})
    meridian = next(row["id"] for row in store.list_users() if row["username"] == "meridian")
    northwind = next(row["id"] for row in store.list_users() if row["username"] == "northwind")
    store.save_extension({"extension": "101", "active": True}, meridian)
    store.save_extension({"extension": "117", "active": True}, northwind)
    store.save_user({"username": "bluewave", "password": "customer-password-1", "role": "user",
                     "email": "b@example.com", "company_name": "Bluewave"})
    bluewave = next(row["id"] for row in store.list_users() if row["username"] == "bluewave")
    store.save_number({"number": "+13025550098", "inbound_extension": "101",
                       "owner_user_id": meridian, "active": True})
    store.save_number({"number": "+13025550011", "inbound_extension": "117",
                       "owner_user_id": northwind, "active": True})
    store.save_number({"number": "+13025550022", "owner_user_id": bluewave, "active": True})
    # A row written before ownership was enforced: a northwind DID pointing at
    # meridian's extension. It rings northwind's own line instead.
    with store._connect() as db:
        db.execute("UPDATE phone_numbers SET inbound_extension='101' WHERE number='+13025550011'")
    # ...and the operator's platform-wide fallback points at northwind too.
    store.set_settings({"inbound_fallback_extension": "117"})
    sync = TelephonyConfigSync(store, DummyAMI(), str(tmp_path / "pjsip.dynamic.conf"))
    dialplan = sync.render_dialplan()

    assert "exten => 13025550098,1,NoOp(Inbound DID 13025550098 owned by extension 101)" in dialplan
    stale = dialplan.split("exten => 13025550011,1,")[1].split("\nexten")[0]
    assert "owned by extension 117" in stale
    assert "Stasis(engineerip,inbound,13025550011,101)" not in dialplan

    # A number whose account has no reachable extension rings nobody: it never
    # borrows another organisation's phone, not even through the platform-wide
    # fallback that points at one.
    unreachable = dialplan.split("exten => 13025550022,1,")[1].split("\nexten")[0]
    assert "has no reachable extension" in unreachable and "Playback(ss-noservice)" in unreachable
    assert "Stasis(engineerip,inbound,13025550022,117)" not in dialplan
