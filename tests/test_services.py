import time
from pathlib import Path

from app.models import Call, CallStore
from app.services import TelephonyService


class DummyAsterisk:
    def __init__(self):
        self.created_call = None
        self.recording_stopped = []
        self.answered = []
        self.played = []
        self.stopped_playbacks = []
        self.inbound_legs = []
        self.inbound_endpoints = []
        self.customer_legs = []
        self.local_legs = []
        self.local_endpoints = []
        self.outbound_endpoints = []
        self.hung_up = []
        self.live_channels = []
        self.forwarded = []
        self.continued = []

    def answer_channel(self, channel_id):
        self.answered.append(channel_id)

    def play_channel_media(self, channel_id, media, playback_id=None):
        self.played.append((channel_id, media, playback_id))
        return {"id": playback_id or "playback"}

    def stop_playback(self, playback_id):
        self.stopped_playbacks.append(playback_id)

    def create_inbound_employee_leg(self, call_id, extension, customer_channel_id, index=0, endpoint=None):
        leg = f"{call_id}-leg-{extension}-{index}"
        self.inbound_legs.append((call_id, extension))
        self.inbound_endpoints.append(endpoint)
        return leg

    def create_outbound_call(self, call_id, extension, phone, provider_endpoint, metadata, endpoint=None, index=0):
        self.created_call = call_id
        self.outbound_endpoints.append(endpoint)
        return f"{call_id}-employee" if int(index) <= 0 else f"{call_id}-employee-{int(index)}"

    def create_customer_leg(self, *args):
        self.customer_legs.append((args[0], args[1], args[2]))
        return f"{args[0]}-customer"

    def create_local_leg(self, call_id, extension, employee_channel_id, caller_id_number=None, endpoint=None, index=0):
        self.local_legs.append((call_id, extension))
        # The channel the platform rings: the extension's own PJSIP endpoint, so
        # a line's 105 is reached by name and a key never reaches Asterisk.
        self.local_endpoints.append(endpoint)
        return f"{call_id}-customer" if int(index) <= 0 else f"{call_id}-customer-{int(index)}"

    def create_forward_leg(self, call_id, phone, provider_endpoint, employee_channel_id, caller_id_number=None, timeout=25, index=1):
        channel = f"{call_id}-customer-{int(index)}"
        self.forwarded.append({"phone": phone, "timeout": int(timeout), "caller_id": caller_id_number})
        self.live_channels.append(channel)
        return channel

    def continue_in_dialplan(self, channel_id, context, extension):
        self.continued.append((channel_id, context, extension))

    def create_bridge(self, call_id):
        return f"bridge-{call_id}"

    def add_channel_to_bridge(self, *args):
        return None

    def start_bridge_recording(self, *args):
        return {}

    def play_bridge_media(self, *args):
        return {}

    def stop_recording(self, name):
        self.recording_stopped.append(name)

    def get_stored_recording(self, name):
        return {"name": name, "filename": f"/var/spool/asterisk/recording/{name}.wav", "format": "wav"}

    def destroy_bridge(self, *args):
        return None

    def hangup(self, *args):
        channel = args[0] if args else ""
        self.hung_up.append(channel)
        self.live_channels = [item for item in self.live_channels if item != channel]

    def hangup_call(self, *args):
        return None

    def cleanup_old_recordings(self, *args):
        return 0

    def list_channels(self):
        return [{"id": channel} for channel in self.live_channels]


def make_service(tmp_path: Path):
    ready = tmp_path / "ari.ready"
    ready.touch()

    class Config:
        ARI_READY_PATH = str(ready)
        CALLS_DB_PATH = str(tmp_path / "calls.db")
        CRM_WEBHOOK_URL = ""
        CRM_WEBHOOK_TOKEN = ""
        DEFAULT_EXTENSION = "101"

    asterisk = DummyAsterisk()
    store = CallStore(str(tmp_path / "calls.db"))
    return TelephonyService(asterisk, config=Config, store=store), asterisk, store


def test_call_is_stored_before_asterisk_originate(tmp_path: Path):
    service, asterisk, store = make_service(tmp_path)
    call = service.start_outbound(phone="+13025551234", extension="101")
    assert call.call_id == asterisk.created_call
    assert store.get(call.call_id).status == "ringing"


def test_recording_finished_event_is_correlated_without_channel(tmp_path: Path):
    service, _, store = make_service(tmp_path)
    call = service.start_outbound(phone="+13025551234", extension="101")
    store.update(call.call_id, recording_name=f"call-{call.call_id}", recording_status="recording")

    service.handle_ari_event({"type": "RecordingFinished", "recording": {"name": f"call-{call.call_id}"}})
    assert store.get(call.call_id).recording_status == "finalized"


def test_webhook_is_bearer_authenticated_and_hmac_signed(tmp_path, monkeypatch):
    service, _, _ = make_service(tmp_path)
    captured = {}

    class Response:
        ok = True
        status_code = 200

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.services.requests.post", fake_post)
    ok, status, error = service._send_webhook("https://crm.example/events", "shared-secret", {"event": "call.test"})
    assert ok is True and status == 200 and error is None
    assert captured["headers"]["Authorization"] == "Bearer shared-secret"
    assert captured["headers"]["X-EngineerIP-Signature"].startswith("sha256=")
    assert captured["headers"]["X-EngineerIP-Timestamp"]
    assert captured["headers"]["X-EngineerIP-Delivery"]


def test_database_webhook_is_queued_and_retried_by_worker(tmp_path, monkeypatch):
    from app.admin import SettingsStore

    service, _, call_store = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    settings.save_webhook({"name": "CRM", "url": "https://crm.example/events", "token": "hook-secret", "events": "*", "active": True})
    service.settings_store = settings
    from app.models import Call
    call = Call(call_id="queued-call", contact_id=None, member_id=None, extension="101", phone="+13025551234")
    call_store.create(call)
    service.notify_crm("call.started", call)
    assert settings.pending_webhook_deliveries()

    class Response:
        ok = True
        status_code = 200

    monkeypatch.setattr("app.services.requests.post", lambda *args, **kwargs: Response())
    assert service.process_webhook_deliveries() >= 1
    assert all(row["status"] == "delivered" for row in settings.list_webhook_deliveries())


def make_customer(settings, username: str, company: str) -> int:
    """A customer account with a carrier behind it, through the same store calls
    the app uses - a number cannot be assigned without one."""
    if not settings.list_providers():
        settings.save_provider({
            "name": "TestCarrier", "server": "sip.test.example", "port": 5060,
            "username": "carrier-user", "password": "carrier-password", "transport": "udp",
            "codecs": "ulaw,alaw", "allowed_ips": "198.51.100.10/32",
        })
    settings.save_user({
        "username": username, "password": "customer-password-01", "role": "user",
        "email": f"{username}@example.com", "full_name": f"{company} Owner",
        "company_name": company, "job_role": "Owner", "phone": "+13025559000",
    })
    return int(next(row["id"] for row in settings.list_users() if row["username"] == username))


def key_on(settings, number: str, digits: str) -> str:
    """The identity of one line's own extension: `101@+13025559901`.

    Every phone number carries its own set from 101 up, so the digits alone never
    name a device - the number they answer on is half of the identity, and it is
    what the store, the dial plan and a call record all use.
    """
    return settings.extension_key_for(number, digits)


def line_setup(settings, number: str, owner: int, extensions) -> dict:
    """Create one line with its own extension set, in dialling order."""
    settings.save_number({
        "number": number, "description": "Main line", "provider": "TestCarrier",
        "owner_user_id": owner, "active": True,
    })
    keys = {}
    for extension in extensions:
        row = settings.add_extension_to_number(
            number, owner, {"extension": extension, "display_name": f"Desk {extension}", "active": True},
        )
        keys[extension] = str(row["key"])
    # The line answers its first extension; that link is how the DID rings a desk.
    settings.save_number({
        "number": number, "description": "Main line", "provider": "TestCarrier",
        "owner_user_id": owner, "inbound_extension": keys[extensions[0]], "active": True,
    })
    return keys


def make_menu_service(tmp_path, extensions=("101", "102", "106"), ivr=None, plan_extra=None):
    """A customer whose main number answers with a menu, and one call already
    waiting on the inbound channel."""
    from app.admin import SettingsStore

    service, asterisk, store = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "menu-co", "Menu Co")
    number = "+13025559901"
    keys = line_setup(settings, number, owner, extensions)
    node = settings.default_ivr_node()
    node.update(ivr or {})
    settings.save_call_route(owner, {
        "phone_number": number, "name": "Main call flow", "active": True,
        "route": {"nodes": [node, *(plan_extra or [])]},
    })
    channel = {"id": "chan-inbound-1", "caller": {"number": "+13025550000"}}
    call = service.start_inbound(channel, number, keys[extensions[0]])
    return service, asterisk, store, settings, owner, number, call


def digits(service, value, channel_id="chan-inbound-1"):
    """Press a keypad, the way ARI reports it."""
    for digit in str(value):
        service.handle_ari_event({"type": "ChannelDtmfReceived", "channel": {"id": channel_id}, "digit": digit})


def test_a_menu_answers_the_call_and_plays_its_prompt(tmp_path):
    service, asterisk, store, settings, _, number, call = make_menu_service(tmp_path)
    assert call is not None
    assert asterisk.answered == ["chan-inbound-1"]                     # taken off ringing
    assert asterisk.played and asterisk.played[0][1] == "sound:custom/ivr-welcome"
    session = service._ivr_sessions_map()["chan-inbound-1"]
    assert session["call_id"] == call.call_id
    # Only this line's own desks, each named by its digits *and* this number: a
    # 101 on another line is a different device, and another customer's desk is
    # not offered at all.
    assert sorted(session["keys"]) == sorted(key_on(settings, number, digits) for digits in ("101", "102", "106"))
    assert sorted(session["extensions"]) == ["101", "102", "106"]      # the digits a caller hears about
    assert call.status == "ringing" and store.get(call.call_id)
    assert service.has_ivr_sessions() is True


def test_the_digits_dial_the_extension_the_caller_entered(tmp_path):
    service, asterisk, store, settings, _, number, call = make_menu_service(tmp_path)
    digits(service, "106")
    # Exactly the desk that was asked for - on the number the call came in on.
    assert asterisk.inbound_legs == [(call.call_id, key_on(settings, number, "106"))]
    assert store.get(call.call_id).extension == key_on(settings, number, "106")
    assert service.has_ivr_sessions() is False                         # the menu is over
    assert asterisk.stopped_playbacks and asterisk.stopped_playbacks[0].startswith("ivr-")


def test_a_digit_that_could_grow_waits_for_the_next_one(tmp_path):
    """"1" cannot dial while 101, 102 and 106 all exist."""
    service, asterisk, _, settings, _, number, call = make_menu_service(tmp_path)
    digits(service, "1")
    assert asterisk.inbound_legs == []                                 # nothing dialled yet
    digits(service, "01")
    assert asterisk.inbound_legs == [(call.call_id, key_on(settings, number, "101"))]


def test_a_pause_on_an_incomplete_extension_asks_again(tmp_path):
    """"10" could still become 101, and once the pause ends it is simply wrong."""
    service, asterisk, _, _, _, _, _ = make_menu_service(tmp_path)
    digits(service, "10")
    assert asterisk.inbound_legs == []                                 # still might become 101
    service.process_ivr_timeouts(now=time.monotonic() + service.IVR_INTERDIGIT + 0.1)
    assert asterisk.inbound_legs == []                                 # nobody was dialled
    assert len(asterisk.played) == 2                                   # and the menu asked again


def test_the_prompt_is_played_again_when_nothing_matches(tmp_path):
    service, asterisk, _, _, _, _, _ = make_menu_service(tmp_path)
    digits(service, "9")
    assert len(asterisk.played) == 2, asterisk.played                 # asked a second time
    assert asterisk.inbound_legs == []


def test_104_is_never_dialled_from_another_line(tmp_path):
    """A menu dials a three-digit extension against the line the call came in on.

    Both of this customer's lines have their own 101; only the second has a 104.
    Dialling 104 from the first line is NOT IN SERVICE - the call never reaches
    across to the other line - while the same digits on the second line ring the
    second line's own 104.
    """
    from app.admin import SettingsStore

    service, asterisk, _ = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "cross-co", "Cross Co")
    first, second = "+13025559901", "+13025559902"
    keys_a = line_setup(settings, first, owner, ("101", "102"))
    keys_b = line_setup(settings, second, owner, ("101", "104"))
    node = settings.default_ivr_node()
    for number in (first, second):
        settings.save_call_route(owner, {
            "phone_number": number, "name": "Main call flow", "active": True,
            "route": {"nodes": [dict(node)]},
        })

    # A call on the first line: it has no 104 of its own, and 104 is not taken
    # from the second line - which holds one - either.
    service.start_inbound({"id": "chan-a", "caller": {"number": "+13025550000"}}, first, keys_a["101"])
    for digit in "104":
        service.handle_ari_event({"type": "ChannelDtmfReceived", "channel": {"id": "chan-a"}, "digit": digit})
    assert asterisk.inbound_legs == [], asterisk.inbound_legs

    # The same digits on the second line ring that line's own 104.
    call_b = service.start_inbound({"channel": {"id": "chan-b", "caller": {"number": "+13025550000"}},
                                    "id": "chan-b"}, second, keys_b["101"])
    for digit in "104":
        service.handle_ari_event({"type": "ChannelDtmfReceived", "channel": {"id": "chan-b"}, "digit": digit})
    assert asterisk.inbound_legs == [(call_b.call_id, keys_b["104"])], asterisk.inbound_legs


def test_a_second_customer_extension_is_never_dialled(tmp_path):
    service, asterisk, store, settings, owner, _, call = make_menu_service(tmp_path, extensions=("101", "102"))
    other = make_customer(settings, "other-co", "Other Co")
    settings.save_extension({"extension": "103", "display_name": "Their desk", "active": True}, other)
    digits(service, "103")
    # 103 is not on the line the call came in on, so nothing at all is dialled -
    # the menu never reaches into another account's desks.
    assert asterisk.inbound_legs == [], asterisk.inbound_legs


def test_after_the_last_attempt_the_nodes_own_fallback_wins(tmp_path):
    service, asterisk, _, settings, _, number, call = make_menu_service(
        tmp_path, ivr={"fallback": "102", "attempts": 2},
        plan_extra=[{"type": "extension", "extension": "106", "label": "Ring 106", "configured": True}])
    digits(service, "9")
    assert len(asterisk.played) == 2                                  # two chances, as configured
    service.process_ivr_timeouts(now=time.monotonic() + 60)
    assert asterisk.inbound_legs == [(call.call_id, key_on(settings, number, "102"))], asterisk.inbound_legs


def test_without_a_fallback_extension_the_rest_of_the_flow_answers(tmp_path):
    service, asterisk, store, settings, _, number, call = make_menu_service(
        tmp_path, plan_extra=[{"type": "extension", "extension": "106", "label": "Ring 106", "configured": True}])
    service.process_ivr_timeouts(now=time.monotonic() + 60)            # attempt one: ask again
    assert service.has_ivr_sessions() is True
    service.process_ivr_timeouts(now=time.monotonic() + 120)           # attempt two: give up
    assert asterisk.inbound_legs == [(call.call_id, key_on(settings, number, "106"))]
    assert store.get(call.call_id).extension == key_on(settings, number, "106")
    assert service.has_ivr_sessions() is False


def test_a_menu_on_its_own_falls_back_to_the_lines_extension(tmp_path):
    """Number -> menu and nothing else: a caller who enters nothing still lands
    on the extension the number belongs to."""
    service, asterisk, store, settings, _, number, call = make_menu_service(tmp_path)
    service.process_ivr_timeouts(now=time.monotonic() + 60)
    service.process_ivr_timeouts(now=time.monotonic() + 120)
    assert asterisk.inbound_legs == [(call.call_id, key_on(settings, number, "101"))]
    assert service.has_ivr_sessions() is False


def test_a_voice_without_a_recording_still_asks_the_caller(tmp_path):
    """The chosen voice is a recording; until it is installed the stock prompt
    stands in, so the menu is never silent."""
    from app.asterisk_client import AsteriskError

    class NoCustomPrompt(DummyAsterisk):
        def play_channel_media(self, channel_id, media, playback_id=None):
            if media != TelephonyService.IVR_STOCK_PROMPT:
                raise AsteriskError("Asterisk API 404")
            return super().play_channel_media(channel_id, media, playback_id)

    service, _, _, settings, _, number, _ = make_menu_service(tmp_path)
    # A second caller, on a client that has no custom recordings at all.
    service.asterisk = NoCustomPrompt()
    fresh = service.start_inbound(
        {"id": "chan-inbound-2", "caller": {"number": "+13025550001"}}, number, key_on(settings, number, "101"),
    )
    assert [media for _, media, _ in service.asterisk.played] == [TelephonyService.IVR_STOCK_PROMPT]
    digits(service, "106", channel_id="chan-inbound-2")
    assert service.asterisk.inbound_legs == [(fresh.call_id, key_on(settings, number, "106"))]


def test_a_caller_who_hangs_up_leaves_no_session_behind(tmp_path):
    service, asterisk, _, _, _, _, call = make_menu_service(tmp_path)
    assert service.has_ivr_sessions() is True
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": "chan-inbound-1"}})
    assert service.has_ivr_sessions() is False
    assert service.process_ivr_timeouts(now=time.monotonic() + 600) == 0


def test_one_extension_never_gets_a_menu_by_itself(tmp_path):
    from app.admin import SettingsStore
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    owner = make_customer(settings, "single", "Single")
    settings.save_extension({"extension": "101", "display_name": "Only desk", "active": True}, owner)
    assert settings.sync_auto_ivr(owner) == []
    assert settings.auto_ivr_wanted(1) is False
    assert settings.auto_ivr_wanted(5) is False


def test_past_five_extensions_the_main_line_gains_the_menu(tmp_path):
    """The menu belongs to the journey of a line, never to a desk.

    A desk's flow is its no-answer policy - ring the desk, then its mailbox.
    Dropping menus on every extension was the panel offering journey steps
    where nothing could run them: the engine answers a DID from the number's
    journey, and a menu on a desk is configuration that never executes.
    """
    from app.admin import SettingsStore
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    owner = make_customer(settings, "bigco", "Big Co")
    number = "+13025559902"
    keys = line_setup(settings, number, owner, ("101", "102", "103", "104", "105"))
    settings.save_call_route(owner, {"phone_number": number, "name": "Main call flow",
                                     "route": settings.default_number_route([keys["101"]]), "active": True})
    settings.ensure_extension_flow(owner, keys["101"])

    assert settings.sync_auto_ivr(owner) == []                          # five desks: the customer's choice
    assert settings.auto_ivr_wanted(6) is True

    sixth = settings.add_extension_to_number(number, owner, {"extension": "106", "display_name": "Desk 106"})
    added = settings.sync_auto_ivr(owner)
    assert added == [f"number {number}"], added                         # the main line, and only it
    for flow in settings.list_call_routes(owner):
        assert flow["route"]["nodes"][0]["type"] == "ivr"
        assert flow["route"]["nodes"][0]["prompt"] == settings.IVR_DEFAULT_PROMPT
        assert flow["route"]["nodes"][1]["type"] != "ivr"
    for flow in settings.list_routing_flows(owner):
        assert all(node["type"] != "ivr" for node in flow["route"]["nodes"])

    # Once again changes nothing - the rule adds, it never duplicates.
    assert settings.sync_auto_ivr(owner) == []
    # A menu the customer wrote is left exactly as it is.
    node = settings.default_ivr_node()
    node["prompt"] = "Custom greeting. Enter an extension."
    settings.save_call_route(owner, {"phone_number": number, "name": "Main call flow",
                                     "route": {"nodes": [node]}, "active": True})
    assert settings.sync_auto_ivr(owner) == []
    assert settings.list_call_routes(owner)[0]["route"]["nodes"][0]["prompt"] == "Custom greeting. Enter an extension."


def test_the_menu_keeps_the_voice_and_the_text_it_was_given(tmp_path):
    from app.admin import SettingsStore
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    owner = make_customer(settings, "voices", "Voices")
    number = "+13025559903"
    keys = line_setup(settings, number, owner, ("101",))
    node = {"type": "ivr", "prompt": "Hola, marque una extension.", "voice": "es-us",
            "input_timeout": 8, "attempts": 3, "fallback": ""}
    settings.save_call_route(owner, {"phone_number": number, "name": "Main", "route": {"nodes": [node]}, "active": True})
    stored = settings.list_call_routes(owner)[0]["route"]["nodes"][0]
    assert (stored["voice"], stored["input_timeout"], stored["attempts"]) == ("es-us", 8, 3)
    plan = settings.inbound_plan(number, keys["101"])
    assert plan["kind"] == "ivr"
    assert plan["media"] == "sound:custom/ivr-welcome-es-us"
    assert plan["prompt"] == "Hola, marque una extension."
    assert plan["extensions"] == [keys["101"]] and plan["destinations"] == []

    # A pasted-over text is trimmed to what the box allows, never rejected.
    settings.save_call_route(owner, {"phone_number": number, "name": "Main",
                                     "route": {"nodes": [{"type": "ivr", "prompt": "y" * 401}]}, "active": True})
    assert len(settings.list_call_routes(owner)[0]["route"]["nodes"][0]["prompt"]) == 400

    # An unknown voice, an endless wait, too many tries and a stranger's desk are
    # refused outright.
    for bad in ({"type": "ivr", "voice": "klingon"}, {"type": "ivr", "input_timeout": 900},
                {"type": "ivr", "attempts": 9}, {"type": "ivr", "fallback": "999"}):
        try:
            settings.save_call_route(owner, {"phone_number": number, "name": "Main",
                                             "route": {"nodes": [bad]}, "active": True})
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_the_menu_defaults_are_filled_in_when_the_canvas_sends_the_basics(tmp_path):
    from app.admin import SettingsStore
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    owner = make_customer(settings, "basic", "Basic")
    number = "+13025559904"
    settings.save_extension({"extension": "101", "display_name": "Desk", "active": True}, owner)
    settings.save_number({"number": number, "provider": "TestCarrier", "owner_user_id": owner,
                          "description": "Line", "inbound_extension": "101"})
    settings.save_call_route(owner, {"phone_number": number, "name": "Main",
                                     "route": {"nodes": [{"type": "ivr"}]}, "active": True})
    stored = settings.list_call_routes(owner)[0]["route"]["nodes"][0]
    assert stored["prompt"] == settings.IVR_DEFAULT_PROMPT
    assert (stored["voice"], stored["input_timeout"], stored["attempts"]) == ("platform", 6, 2)
    assert settings.ivr_voices() and settings.ivr_voices()[0]["id"] == "platform"


def test_recording_needs_both_switches_to_agree(tmp_path):
    """The customer's per-extension switch decides, and the platform switch can
    stop everything: neither one replaces the other."""
    service, _, _ = make_service(tmp_path)

    class Settings:
        def __init__(self, extension_enabled, extension_active=1, platform_enabled=True):
            self.extension_enabled = extension_enabled
            self.extension_active = extension_active
            self.platform_enabled = platform_enabled
        def recording_platform_enabled(self):
            return self.platform_enabled
        def get_settings(self):
            return {"recording_enabled": "true" if self.platform_enabled else "false"}
        def list_extensions(self):
            return [{"extension": "101", "active": self.extension_active,
                     "recording_enabled": int(self.extension_enabled)}]

    # Both on: the device records.
    service.settings_store = Settings(True)
    assert service._recording_settings("101")["enabled"] is True
    # The customer switched this device off: it does not record.
    service.settings_store = Settings(False)
    assert service._recording_settings("101")["enabled"] is False
    # The platform switch off is a veto: a customer's opt-in cannot override it,
    # and the sheet still reports what the customer chose.
    service.settings_store = Settings(True, platform_enabled=False)
    vetoed = service._recording_settings("101")
    assert vetoed["enabled"] is False
    assert vetoed["platform_enabled"] is False and vetoed["extension_enabled"] is True
    # A disabled or unknown extension never records.
    service.settings_store = Settings(True, extension_active=0)
    assert service._recording_settings("101")["enabled"] is False
    assert service._recording_settings("999")["enabled"] is False
    # Switching the platform back on restores the customer's own decision.
    service.settings_store = Settings(True)
    assert service._recording_settings("101")["enabled"] is True


def test_one_of_the_customers_own_numbers_is_called_without_a_carrier(tmp_path):
    """The field case: a phone on +13025550098 calls +13025550067.

    The number belongs to the customer making the call, so the platform looks it
    up to the extension it is set to ring and originates that leg itself. The
    call never reaches the carrier - which is what makes a number-to-number call
    within one account dependable rather than a round trip that a carrier may
    refuse, loop back or bill for.
    """
    from app.admin import SettingsStore
    from app.models import Call

    service, asterisk, store = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "field-co", "Field Co")
    a, b = "+13025550098", "+13025550067"
    first = line_setup(settings, a, owner, ("105",))
    second = line_setup(settings, b, owner, ("117",))

    # The call comes from +13025550098's own 105 and dials the account's other
    # number: it lands on *that* number's extension, the 117 on +13025550067.
    store.create(Call(call_id="local-1", contact_id=None, member_id=None,
                      extension=first["105"], phone=b, provider="TestCarrier"))
    service._start_customer(store.get("local-1"))

    assert asterisk.local_legs == [("local-1", second["117"])]
    assert asterisk.local_endpoints == [f"PJSIP/117-13025550067"]
    assert asterisk.customer_legs == []
    assert store.get("local-1").status == "dialing_customer"


def test_a_number_the_platform_does_not_own_still_leaves_through_the_carrier(tmp_path):
    """Nothing about the local shortcut changes ordinary outbound calls.

    A number that is not on this platform at all is an external call: it is
    dialled over the customer's carrier, with the customer's caller ID.
    """
    from app.admin import SettingsStore
    from app.models import Call

    service, asterisk, store = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "field-co", "Field Co")
    mine = line_setup(settings, "+13025550098", owner, ("105",))
    settings.save_number({"number": "+13025550098", "provider": "TestCarrier", "inbound_extension": mine["105"],
                          "owner_user_id": owner, "default_outbound": True, "active": True})

    store.create(Call(call_id="outside-1", contact_id=None, member_id=None, extension=mine["105"],
                      phone="+13025559999", provider="TestCarrier", caller_id_number="+13025550098"))
    service._start_customer(store.get("outside-1"))
    assert asterisk.customer_legs[-1][:2] == ("outside-1", "+13025559999")
    assert asterisk.customer_legs[-1][2].startswith("provider-")
    assert asterisk.local_legs == []


def test_another_customers_platform_number_is_reached_inside_the_platform(tmp_path):
    """Typing a full number the platform owns reaches it, whoever holds it.

    +13025550011 belongs to another customer. Dialling it from a desk of
    +13025550098 rings that number's own inbound destination on the platform -
    never the carrier - and does not expose the other customer's other
    extensions (see the dial plan tests for the `<number>*<digits>` refusal).
    """
    from app.admin import SettingsStore
    from app.models import Call

    service, asterisk, store = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "field-co", "Field Co")
    other = make_customer(settings, "other-co", "Other Co")
    mine = line_setup(settings, "+13025550098", owner, ("105",))
    theirs = line_setup(settings, "+13025550011", other, ("201", "202"))

    store.create(Call(call_id="cross-1", contact_id=None, member_id=None, extension=mine["105"],
                      phone="+13025550011", provider="TestCarrier", caller_id_number="+13025550098"))
    service._start_customer(store.get("cross-1"))

    assert asterisk.local_legs == [("cross-1", theirs["201"])]
    assert asterisk.local_endpoints == ["PJSIP/201-13025550011"]
    assert asterisk.customer_legs == []


def test_a_platform_number_with_no_reachable_extension_is_not_in_service(tmp_path):
    """A full number the platform owns but cannot ring is NOT IN SERVICE.

    It must never fall back to the carrier: the number is ours, so a carrier
    round trip would be a second, different answer for the same number.
    """
    from app.admin import SettingsStore
    from app.models import Call

    service, asterisk, store = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "field-co", "Field Co")
    other = make_customer(settings, "other-co", "Other Co")
    mine = line_setup(settings, "+13025550098", owner, ("105",))
    settings.save_number({"number": "+13025550012", "provider": "TestCarrier", "owner_user_id": other,
                          "active": True})                       # owned, but no extension at all
    hung = []
    asterisk.hangup = lambda channel_id: hung.append(channel_id)

    store.create(Call(call_id="empty-1", contact_id=None, member_id=None, extension=mine["105"],
                      phone="+13025550012", provider="TestCarrier", caller_id_number="+13025550098",
                      employee_channel_id="empty-1-employee"))
    service._start_customer(store.get("empty-1"))

    failed = store.get("empty-1")
    assert failed.status == "failed"
    assert asterisk.customer_legs == []
    assert asterisk.local_legs == []
    assert hung == ["empty-1-employee"]


def test_a_stale_link_on_an_own_number_rings_the_accounts_own_extension(tmp_path):
    """A number of this account left pointing at another organisation's phone.

    The panel must not take that row as a shortcut to a stranger: this line's own
    extension answers instead, which is what an inbound call on that number would
    do as well.
    """
    from app.admin import SettingsStore
    from app.models import Call

    service, asterisk, store = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "field-co", "Field Co")
    other = make_customer(settings, "other-co", "Other Co")
    line_setup(settings, "+13025550067", owner, ("105", "117"))
    line_setup(settings, "+13025550011", other, ("201",))
    # A row that survived from before ownership was enforced: this account's
    # number pointing at another organisation's extension.
    with settings._connect() as db:
        db.execute("UPDATE phone_numbers SET inbound_extension='201' WHERE number='+13025550067'")

    store.create(Call(call_id="stale-1", contact_id=None, member_id=None,
                      extension=key_on(settings, "+13025550067", "105"),
                      phone="+13025550067", provider="TestCarrier"))
    service._start_customer(store.get("stale-1"))

    # The number is this account's own, so the call never leaves through the
    # carrier - and it rings this line's own desk, never the stranger's 201.
    assert asterisk.local_legs == [("stale-1", key_on(settings, "+13025550067", "105"))]
    assert asterisk.customer_legs == []


def test_an_extension_with_no_line_of_its_own_cannot_borrow_the_accounts_line(tmp_path):
    """The panel path for the same case, and there is no account-level fallback.

    Caller ID comes from the phone-number context, so an extension that has no
    number of its own has nothing to present: the panel is refused rather than
    showing the account's main line, and it never borrows another account's
    number either. Naming one of the account's own lines still works - that is
    how an extension added later calls out.
    """
    import pytest

    from app.admin import SettingsStore

    service, asterisk, _ = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "single-line", "Single Line")
    other = make_customer(settings, "other-line", "Other Line")
    settings.save_number({"number": "+13022661626", "provider": "TestCarrier", "inbound_extension": "101",
                          "owner_user_id": owner, "default_outbound": True, "active": True})
    settings.save_number({"number": "+13025550099", "provider": "TestCarrier", "inbound_extension": "102",
                          "owner_user_id": owner, "active": True})
    settings.save_number({"number": "+13025550011", "provider": "TestCarrier", "inbound_extension": "201",
                          "owner_user_id": other, "active": True})
    settings.add_extension_to_number("+13022661626", owner, {"extension": "101", "active": True})
    settings.add_extension_to_number("+13025550099", owner, {"extension": "102", "active": True})
    settings.add_extension_to_number("+13025550011", other, {"extension": "201", "active": True})
    # A device that is on a line presents that line without being asked.
    assert settings.get_outbound_number(key_on(settings, "+13025550099", "102"))["number"] == "+13025550099"

    # The first line is taken down, and 101 keeps its row with no number - which
    # is what an operator sees in the console as "No number yet".
    settings.delete_number("+13022661626", cascade=False)
    assert settings.get_outbound_number("101") is None
    with pytest.raises(RuntimeError):
        service.start_outbound(phone="+13025559999", extension="101")
    assert asterisk.created_call is None
    # Another account's number is refused, named or not: 101 is not theirs.
    assert settings.get_outbound_number("101", "+13025550011") is None
    # Naming one of the extension's own account's lines is how it calls out: the
    # line is the caller ID, and it is a deliberate choice rather than a fallback.
    assert settings.get_outbound_number("101", "+13025550099")["number"] == "+13025550099"
    call = service.start_outbound(phone="+13025559999", extension="101", caller_id_number="+13025550099")
    assert call.caller_id_number == "+13025550099"
    assert asterisk.created_call == call.call_id


def test_endpoint_states_are_read_once_per_few_seconds_and_never_guessed(tmp_path):
    """Console refreshes must not hammer Asterisk, and must not invent states.

    A phone that is not registered is the first thing to check when a call does
    not ring, so the answer is cached briefly - and when Asterisk cannot be
    asked, the console is told nothing rather than "nothing is registered".
    """
    service, _, _ = make_service(tmp_path)
    calls = []

    class Endpoints:
        def list_endpoints(self):
            calls.append(1)
            return [
                {"technology": "pjsip", "resource": "101", "state": "online"},
                {"technology": "chan_sip", "resource": "999", "state": "online"},
            ]

    service.asterisk = Endpoints()
    assert service.endpoint_states() == {"101": "online"}
    assert service.endpoint_states() == {"101": "online"}
    assert len(calls) == 1                       # served from the cache

    service._endpoint_states = (0.0, {})         # ...until the window passes
    assert service.endpoint_states() == {"101": "online"}
    assert len(calls) == 2

    class Broken:
        def list_endpoints(self):
            raise RuntimeError("ARI down")

    service._endpoint_states = (0.0, {})
    service.asterisk = Broken()
    assert service.endpoint_states() == {}


def test_an_extension_answering_in_the_browser_is_rung_on_both_endpoints(tmp_path):
    """The panel follows the same rule the dial plan does.

    Media is decided by the endpoint a call is placed towards, and the two
    device kinds never share one identity: the browser signs in as
    `<username>-webrtc` on its WebRTC endpoint, a desk phone or Zoiper as
    `<username>` on the plain one. An extension marked as answering in the
    browser rings BOTH - when the panel dials out for it and when a caller
    rings it - so a Zoiper on a WebRTC-ticked extension is rung with the media
    it speaks and can actually answer.
    """
    from app.admin import SettingsStore

    service, asterisk, store = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "browser-co", "Browser Co")
    number = "+13025550077"
    keys = line_setup(settings, number, owner, ("101", "102"))
    settings.save_extension({"extension": keys["101"], "webrtc_enabled": True}, owner)
    settings.save_number({"number": number, "provider": "TestCarrier", "inbound_extension": keys["101"],
                          "owner_user_id": owner, "default_outbound": True, "active": True})
    web = next(row["sip_username"] for row in settings.list_extensions() if row["digits"] == "101")

    # The digits name one row of this account, so they are read exactly; the row
    # behind them is the browser one, whose WebRTC endpoint rings beside the
    # plain one.
    assert service.dial_endpoint(keys["101"]) == f"PJSIP/{web}-webrtc"
    assert service.dial_endpoints("101") == [f"PJSIP/{web}-webrtc", "PJSIP/101-13025550077"]
    assert service.dial_endpoint(keys["102"]) == "PJSIP/102-13025550077"
    assert service.dial_endpoints(keys["102"]) == ["PJSIP/102-13025550077"]

    service.start_outbound(phone="+13025559999", extension=keys["101"])
    assert asterisk.outbound_endpoints == [f"PJSIP/{web}-webrtc", "PJSIP/101-13025550077"]
    started = store.get(next(c.call_id for c in store.all()))
    assert started.employee_channel_ids == f"{started.call_id}-employee|{keys['101']},{started.call_id}-employee-1|{keys['101']}"

    asterisk.inbound_endpoints.clear()
    service.start_inbound({"id": "channel-browser", "caller": {"number": "+13025550000"}}, number, keys["101"])
    assert asterisk.inbound_endpoints == [f"PJSIP/{web}-webrtc", "PJSIP/101-13025550077"]


def test_the_first_device_to_answer_wins_and_the_other_stays_silent_on_the_line(tmp_path):
    """Two devices of one extension ring; the loser's cleanup never drops the call.

    A WebRTC extension's browser and plain phone are two legs of one call.
    Whoever answers first takes it and the other leg is cancelled - and when
    the cancelled leg's destroy event arrives later, it is bookkeeping: tearing
    the live call down there used to drop the answered call.
    """
    from app.admin import SettingsStore

    service, asterisk, store = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "browser-co", "Browser Co")
    number = "+13025550077"
    keys = line_setup(settings, number, owner, ("101",))
    settings.save_extension({"extension": keys["101"], "webrtc_enabled": True}, owner)
    settings.save_number({"number": number, "provider": "TestCarrier", "inbound_extension": keys["101"],
                          "owner_user_id": owner, "default_outbound": True, "active": True})
    web = next(row["sip_username"] for row in settings.list_extensions() if row["digits"] == "101")

    asterisk.live_channels = ["caller-1"]
    service.start_inbound({"id": "caller-1", "caller": {"number": "+13025550000"}}, number, keys["101"])
    call = store.get(next(c.call_id for c in store.all()))
    legs = [leg for leg, _ in service._leg_pairs(call)]
    web_leg, plain_leg = legs[0], legs[1]
    asterisk.live_channels += [web_leg, plain_leg]

    # The plain phone answers first; the browser leg stops ringing.
    service.handle_ari_event({"type": "ChannelStateChange", "channel": {"id": plain_leg, "state": "Up"}})
    call = store.get(call.call_id)
    assert call.employee_channel_id == plain_leg
    assert call.status in {"employee_answered", "answered"}
    assert web_leg in asterisk.hung_up

    # The cancelled browser leg's destroy is bookkeeping, not a hangup.
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": web_leg}})
    call = store.get(call.call_id)
    assert call.status not in {"failed"}
    assert call.ended_at is None


def _bridged_setup(tmp_path: Path):
    """A call whose employee answered: two other desks still ring."""
    service, asterisk, store = make_service(tmp_path)
    call = Call(
        call_id="cut-1", contact_id=None, member_id=None,
        extension="main-1", phone="+13025551234", direction="inbound", status="ringing",
        employee_channel_id="leg-answer",
        employee_channel_ids="leg-answer|101,leg-two|102,leg-three|103",
        customer_channel_id="cust-1",
    )
    store.create(call)
    for channel in ("leg-answer", "leg-two", "leg-three", "cust-1"):
        service._leg_index[channel] = call.call_id
        asterisk.live_channels.append(channel)
    return service, asterisk, store, call


def test_a_recording_that_cannot_write_never_hangs_up_the_live_call(tmp_path: Path):
    service, asterisk, store, call = _bridged_setup(tmp_path)

    def broken_recording(*args):
        raise RuntimeError("Unable to open recording file: Permission denied")

    asterisk.start_bridge_recording = broken_recording
    service._recording_settings = lambda extension: {
        "enabled": True, "format": "wav", "beep": False,
        "max_duration": 60, "announcement": False, "announcement_media": "",
    }
    service.handle_ari_event({
        "type": "ChannelStateChange",
        "channel": {"id": "leg-answer", "state": "Up"},
        "timestamp": "2026-10-17T10:00:00Z",
    })
    updated = store.get(call.call_id)
    # The bridge stands and the people keep talking; only the recording failed.
    # The only hangups are the losing sibling desks the answerer beat.
    assert updated.bridge_id == "bridge-cut-1"
    assert updated.recording_status == "failed"
    assert set(asterisk.hung_up) == {"leg-two", "leg-three"}
    assert "leg-answer" not in asterisk.hung_up
    assert "cust-1" not in asterisk.hung_up


def test_a_bridge_that_cannot_build_takes_down_every_leg_of_the_call(tmp_path: Path):
    service, asterisk, store, call = _bridged_setup(tmp_path)

    def broken_bridge(*args):
        raise RuntimeError("bridge is gone")

    asterisk.add_channel_to_bridge = broken_bridge
    service.handle_ari_event({
        "type": "ChannelStateChange",
        "channel": {"id": "leg-answer", "state": "Up"},
        "timestamp": "2026-10-17T10:00:00Z",
    })
    # Every leg of the failed call goes down - not just the one that answered.
    # Leftover ringing legs answer into a void and cut the instant they pick up.
    assert {"cust-1", "leg-answer", "leg-two", "leg-three"} <= set(asterisk.hung_up)
    assert store.get(call.call_id).status == "failed"


def test_a_call_that_ends_hangs_up_every_leg_that_still_rings(tmp_path: Path):
    service, asterisk, store, call = _bridged_setup(tmp_path)
    service._finalize(call.call_id, "test_over")
    # A finished call owns nothing: no desk of it may keep ringing. Without
    # this sweep a call that ends leaves its devices ringing forever, and each
    # answered leftover cuts the instant somebody picks up.
    assert {"cust-1", "leg-answer", "leg-two", "leg-three"} <= set(asterisk.hung_up)
    assert store.get(call.call_id).status in {"completed", "failed"}


def test_a_straggler_of_a_finished_call_is_hung_up_not_bridged(tmp_path: Path):
    service, asterisk, store, call = _bridged_setup(tmp_path)
    store.update(call.call_id, status="failed", ended_at="2026-10-17T10:05:00Z")
    service.handle_ari_event({
        "type": "ChannelStateChange",
        "channel": {"id": "leg-two", "state": "Up"},
        "timestamp": "2026-10-17T10:05:30Z",
    })
    updated = store.get(call.call_id)
    assert asterisk.hung_up == ["leg-two"]          # the straggler goes down
    assert updated.status == "failed"               # no resurrection of the call
    assert updated.bridge_id is None                # and no bridge was built


def test_the_customer_giving_up_takes_down_every_desk_it_was_ringing(tmp_path: Path):
    service, asterisk, store, call = _bridged_setup(tmp_path)
    service.handle_ari_event({
        "type": "ChannelDestroyed",
        "channel": {"id": "cust-1", "name": "PJSIP/trunk-00000001"},
        "timestamp": "2026-10-17T10:01:00Z",
    })
    # Every desk of the call goes down - the first leg alone is not enough.
    assert {"leg-answer", "leg-two", "leg-three"} <= set(asterisk.hung_up)


# --------------------------------------------------------------- routing logic
# The journey of a line - menu, hours, voicemail, forwarding - belongs to the
# main number; a desk keeps only its no-answer policy. These tests hold that
# split together: where the steps live, and that the engine runs them.


def _routing_setup(tmp_path: Path, extensions=("101", "102")):
    from app.admin import SettingsStore
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    owner = make_customer(settings, "route-co", "Route Co")
    number = "+13025559904"
    keys = line_setup(settings, number, owner, extensions)
    return settings, owner, number, keys


def _seed_legacy_desk_flow(settings, owner, key, nodes):
    """Write a desk flow the way the old panel did - straight to the table.

    The split between journey and desk policy is enforced on every save now, so
    rows like these can only come from a deployment that ran the old UI. That
    is exactly the data the compatibility layer has to keep working.
    """
    import json
    route = json.dumps({"nodes": nodes})
    with settings._connect() as db:
        db.execute(
            "INSERT INTO routing_flows(owner_user_id,target_type,target,name,route_json,active) VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(owner_user_id,target_type,target) DO UPDATE SET route_json=excluded.route_json",
            (int(owner), "extension", str(key), "Extension call flow", route),
        )


def test_a_desk_flow_refuses_a_menu(tmp_path: Path):
    settings, owner, _, keys = _routing_setup(tmp_path)
    try:
        settings.save_routing_flow(owner, {
            "name": "Desk", "route": {"nodes": [settings.default_ivr_node()]}, "active": True,
        }, target_type="extension", target=keys["101"])
    except ValueError as error:
        assert "main number" in str(error)
    else:
        raise AssertionError("a menu on a desk must be refused")


def test_the_journey_configured_on_the_desk_still_answers_the_line(tmp_path: Path):
    """Steps the panel put on a desk keep working until they are moved.

    The routing canvas offered menus and hours on every extension, so that is
    where customers built their line's journey - and then "the IVR does not
    work", because the engine runs the number's journey. The desk the line
    answers with fills in whatever the number flow leaves silent.
    """
    settings, owner, number, keys = _routing_setup(tmp_path)
    settings.save_call_route(owner, {
        "phone_number": number, "name": "Main call flow", "active": True,
        "route": settings.default_number_route([keys["101"], keys["102"]]),
    })
    menu = settings.default_ivr_node()
    menu["prompt"] = "Welcome to the front desk. Enter an extension."
    _seed_legacy_desk_flow(settings, owner, keys["101"],
        [menu, {"type": "voicemail", "mailbox": keys["101"], "configured": True}])

    plan = settings.inbound_plan(number, keys["101"])
    assert plan["kind"] == "ivr"
    assert plan["prompt"] == "Welcome to the front desk. Enter an extension."
    assert plan["voicemail"] == keys["101"]


def test_the_journey_on_the_number_wins_over_the_desks(tmp_path: Path):
    settings, owner, number, keys = _routing_setup(tmp_path)
    menu = settings.default_ivr_node()
    menu["prompt"] = "The line's own greeting."
    settings.save_call_route(owner, {
        "phone_number": number, "name": "Main call flow", "active": True,
        "route": {"nodes": [menu, {"type": "ring_group", "extensions": [keys["101"]], "timeout": 25, "configured": True}]},
    })
    desk_menu = settings.default_ivr_node()
    desk_menu["prompt"] = "A desk greeting that must not run."
    _seed_legacy_desk_flow(settings, owner, keys["101"], [desk_menu])

    plan = settings.inbound_plan(number, keys["101"])
    assert plan["kind"] == "ivr"
    assert plan["prompt"] == "The line's own greeting."


def test_the_desks_mailbox_answers_when_the_journey_is_silent(tmp_path: Path):
    settings, owner, number, keys = _routing_setup(tmp_path)
    settings.save_call_route(owner, {
        "phone_number": number, "name": "Main call flow", "active": True,
        "route": settings.default_number_route([keys["101"]]),
    })
    settings.save_extension({"extension": keys["101"], "voicemail_enabled": True, "voicemail_pin": "2468"}, owner)

    plan = settings.inbound_plan(number, keys["101"])
    assert plan["voicemail"] == keys["101"]          # a ringed-out desk keeps its messages


def test_the_voicemail_step_is_the_decision_to_take_messages(tmp_path: Path):
    """Answering "Voice Mail" in the journey is itself the switch.

    Before, a mailbox also needed a separate toggle buried in the SIP sheet -
    without it the step pointed at a mailbox the renderer never wrote, and the
    call was dropped instead ("the voicemail function is not working").
    """
    settings, owner, number, keys = _routing_setup(tmp_path)
    settings.save_call_route(owner, {
        "phone_number": number, "name": "Main call flow", "active": True,
        "route": {"nodes": [
            {"type": "ring_group", "extensions": [keys["101"], keys["102"]], "timeout": 25, "configured": True},
            {"type": "voicemail", "mailbox": keys["102"], "configured": True},
        ]},
    })
    assert settings.extension_voicemail_enabled(keys["102"]) is False   # the switch stays off

    plan = settings.inbound_plan(number, keys["101"])
    assert plan["voicemail"] == keys["102"]


def test_when_nobody_answers_the_forward_number_is_rung(tmp_path: Path):
    service, asterisk, store = make_service(tmp_path)
    from app.admin import SettingsStore
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "fwd-co", "Fwd Co")
    number = "+13025559905"
    keys = line_setup(settings, number, owner, ("101",))
    settings.save_call_route(owner, {
        "phone_number": number, "name": "Main call flow", "active": True,
        "route": {"nodes": [
            {"type": "ring_group", "extensions": [keys["101"]], "timeout": 25, "configured": True},
            {"type": "forward", "phone": "+13025550199", "timeout": 40, "configured": True},
        ]},
    })
    channel = {"id": "chan-inbound-fwd", "caller": {"number": "+13025550000"}}
    call = service.start_inbound(channel, number, keys["101"])
    assert call is not None
    leg = call.employee_channel_id

    # The desk rings out; instead of killing the caller, the journey's forward
    # number is dialled with the caller still waiting on the line.
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": leg}})
    assert asterisk.forwarded and asterisk.forwarded[0]["phone"] == "+13025550199"
    assert asterisk.forwarded[0]["timeout"] == 40
    assert "chan-inbound-fwd" not in asterisk.hung_up
    updated = store.get(call.call_id)
    assert updated.status == "forwarding"
    assert updated.employee_channel_id in updated.employee_channel_ids

    # A forward that dies unanswered ends the call - and never forwards twice.
    forward_channel = updated.employee_channel_id
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": forward_channel}})
    assert len(asterisk.forwarded) == 1
    assert "chan-inbound-fwd" in asterisk.hung_up
    assert store.get(call.call_id).status in {"completed", "failed"}


def test_a_forward_answer_bridges_the_waiting_caller(tmp_path: Path):
    service, asterisk, store = make_service(tmp_path)
    from app.admin import SettingsStore
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "fwd2-co", "Fwd2 Co")
    number = "+13025559906"
    keys = line_setup(settings, number, owner, ("101",))
    settings.save_call_route(owner, {
        "phone_number": number, "name": "Main call flow", "active": True,
        "route": {"nodes": [
            {"type": "ring_group", "extensions": [keys["101"]], "timeout": 25, "configured": True},
            {"type": "forward", "phone": "+13025550199", "timeout": 25, "configured": True},
        ]},
    })
    channel = {"id": "chan-inbound-fwd2", "caller": {"number": "+13025550000"}}
    call = service.start_inbound(channel, number, keys["101"])
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": call.employee_channel_id}})
    forward_channel = store.get(call.call_id).employee_channel_id

    # The outside number picks up: it is bridged to the waiting caller exactly
    # like a desk that answered.
    service.handle_ari_event({
        "type": "ChannelStateChange",
        "channel": {"id": forward_channel, "state": "Up"},
        "timestamp": "2026-10-18T10:00:00Z",
    })
    updated = store.get(call.call_id)
    assert updated.bridge_id == f"bridge-{call.call_id}"
    assert updated.answered is True
