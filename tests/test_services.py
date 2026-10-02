import time
from pathlib import Path

from app.models import CallStore
from app.services import TelephonyService


class DummyAsterisk:
    def __init__(self):
        self.created_call = None
        self.recording_stopped = []
        self.answered = []
        self.played = []
        self.stopped_playbacks = []
        self.inbound_legs = []

    def answer_channel(self, channel_id):
        self.answered.append(channel_id)

    def play_channel_media(self, channel_id, media, playback_id=None):
        self.played.append((channel_id, media, playback_id))
        return {"id": playback_id or "playback"}

    def stop_playback(self, playback_id):
        self.stopped_playbacks.append(playback_id)

    def create_inbound_employee_leg(self, call_id, extension, customer_channel_id, index=0):
        leg = f"{call_id}-leg-{extension}-{index}"
        self.inbound_legs.append((call_id, extension))
        return leg

    def create_outbound_call(self, call_id, extension, phone, provider_endpoint, metadata):
        self.created_call = call_id
        return call_id

    def create_customer_leg(self, *args):
        return f"{args[0]}-customer"

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
        return None

    def hangup_call(self, *args):
        return None

    def cleanup_old_recordings(self, *args):
        return 0

    def list_channels(self):
        return []


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


def make_menu_service(tmp_path, extensions=("101", "102", "106"), ivr=None, plan_extra=None):
    """A customer whose main number answers with a menu, and one call already
    waiting on the inbound channel."""
    from app.admin import SettingsStore

    service, asterisk, store = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "menu-co", "Menu Co")
    number = "+13025559901"
    for extension in extensions:
        settings.save_extension({"extension": extension, "display_name": f"Desk {extension}", "active": True}, owner)
    settings.save_number({
        "number": number, "description": "Main line", "provider": "TestCarrier",
        "owner_user_id": owner, "inbound_extension": extensions[0], "active": True,
    })
    node = settings.default_ivr_node()
    node.update(ivr or {})
    settings.save_call_route(owner, {
        "phone_number": number, "name": "Main call flow", "active": True,
        "route": {"nodes": [node, *(plan_extra or [])]},
    })
    channel = {"id": "chan-inbound-1", "caller": {"number": "+13025550000"}}
    call = service.start_inbound(channel, number, extensions[0])
    return service, asterisk, store, settings, owner, number, call


def digits(service, value, channel_id="chan-inbound-1"):
    """Press a keypad, the way ARI reports it."""
    for digit in str(value):
        service.handle_ari_event({"type": "ChannelDtmfReceived", "channel": {"id": channel_id}, "digit": digit})


def test_a_menu_answers_the_call_and_plays_its_prompt(tmp_path):
    service, asterisk, store, _, _, _, call = make_menu_service(tmp_path)
    assert call is not None
    assert asterisk.answered == ["chan-inbound-1"]                     # taken off ringing
    assert asterisk.played and asterisk.played[0][1] == "sound:custom/ivr-welcome"
    session = service._ivr_sessions_map()["chan-inbound-1"]
    assert session["call_id"] == call.call_id
    assert sorted(session["extensions"]) == ["101", "102", "106"]      # only this customer's desks
    assert call.status == "ringing" and store.get(call.call_id)
    assert service.has_ivr_sessions() is True


def test_the_digits_dial_the_extension_the_caller_entered(tmp_path):
    service, asterisk, store, _, _, _, call = make_menu_service(tmp_path)
    digits(service, "106")
    assert asterisk.inbound_legs == [(call.call_id, "106")]            # exactly the desk that was asked for
    assert store.get(call.call_id).extension == "106"
    assert service.has_ivr_sessions() is False                         # the menu is over
    assert asterisk.stopped_playbacks and asterisk.stopped_playbacks[0].startswith("ivr-")


def test_a_digit_that_could_grow_waits_for_the_next_one(tmp_path):
    """"1" cannot dial while 101, 102 and 106 all exist."""
    service, asterisk, _, _, _, _, call = make_menu_service(tmp_path)
    digits(service, "1")
    assert asterisk.inbound_legs == []                                 # nothing dialled yet
    digits(service, "01")
    assert asterisk.inbound_legs == [(call.call_id, "101")]


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


def test_a_second_customer_extension_is_never_dialled(tmp_path):
    service, asterisk, store, settings, owner, _, call = make_menu_service(tmp_path, extensions=("101", "102"))
    other = make_customer(settings, "other-co", "Other Co")
    settings.save_extension({"extension": "103", "display_name": "Their desk", "active": True}, other)
    digits(service, "103")
    assert all(extension != "103" for _, extension in asterisk.inbound_legs), asterisk.inbound_legs


def test_after_the_last_attempt_the_nodes_own_fallback_wins(tmp_path):
    service, asterisk, _, _, _, _, call = make_menu_service(
        tmp_path, ivr={"fallback": "102", "attempts": 2},
        plan_extra=[{"type": "extension", "extension": "106", "label": "Ring 106", "configured": True}])
    digits(service, "9")
    assert len(asterisk.played) == 2                                  # two chances, as configured
    service.process_ivr_timeouts(now=time.monotonic() + 60)
    assert asterisk.inbound_legs == [(call.call_id, "102")], asterisk.inbound_legs


def test_without_a_fallback_extension_the_rest_of_the_flow_answers(tmp_path):
    service, asterisk, store, _, _, _, call = make_menu_service(
        tmp_path, plan_extra=[{"type": "extension", "extension": "106", "label": "Ring 106", "configured": True}])
    service.process_ivr_timeouts(now=time.monotonic() + 60)            # attempt one: ask again
    assert service.has_ivr_sessions() is True
    service.process_ivr_timeouts(now=time.monotonic() + 120)           # attempt two: give up
    assert asterisk.inbound_legs == [(call.call_id, "106")]
    assert store.get(call.call_id).extension == "106"
    assert service.has_ivr_sessions() is False


def test_a_menu_on_its_own_falls_back_to_the_lines_extension(tmp_path):
    """Number -> menu and nothing else: a caller who enters nothing still lands
    on the extension the number belongs to."""
    service, asterisk, store, _, _, _, call = make_menu_service(tmp_path)
    service.process_ivr_timeouts(now=time.monotonic() + 60)
    service.process_ivr_timeouts(now=time.monotonic() + 120)
    assert asterisk.inbound_legs == [(call.call_id, "101")]
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

    service, _, _, _, _, _, _ = make_menu_service(tmp_path)
    # A second caller, on a client that has no custom recordings at all.
    service.asterisk = NoCustomPrompt()
    fresh = service.start_inbound({"id": "chan-inbound-2", "caller": {"number": "+13025550001"}}, "+13025559901", "101")
    assert [media for _, media, _ in service.asterisk.played] == [TelephonyService.IVR_STOCK_PROMPT]
    digits(service, "106", channel_id="chan-inbound-2")
    assert service.asterisk.inbound_legs == [(fresh.call_id, "106")]


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


def test_past_five_extensions_every_flow_gains_the_menu(tmp_path):
    from app.admin import SettingsStore
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    owner = make_customer(settings, "bigco", "Big Co")
    number = "+13025559902"
    for extension in ("101", "102", "103", "104", "105"):
        settings.save_extension({"extension": extension, "display_name": f"Desk {extension}", "active": True}, owner)
    settings.save_number({"number": number, "provider": "TestCarrier", "owner_user_id": owner,
                          "description": "Main line", "inbound_extension": "101"})
    settings.save_call_route(owner, {"phone_number": number, "name": "Main call flow",
                                     "route": settings.default_number_route(["101"]), "active": True})
    settings.ensure_extension_flow(owner, "101")

    assert settings.sync_auto_ivr(owner) == []                          # five desks: the customer's choice
    assert settings.auto_ivr_wanted(6) is True

    settings.save_extension({"extension": "106", "display_name": "Desk 106", "active": True}, owner)
    added = settings.sync_auto_ivr(owner)
    assert f"number {number}" in added and "extension 106" in added, added
    assert len(added) >= 6, added                                       # the number, and every desk, menu first
    for flow in settings.list_call_routes(owner):
        assert flow["route"]["nodes"][0]["type"] == "ivr"
        assert flow["route"]["nodes"][0]["prompt"] == settings.IVR_DEFAULT_PROMPT
        assert flow["route"]["nodes"][1]["type"] != "ivr"
    for flow in settings.list_routing_flows(owner):
        assert flow["route"]["nodes"][0]["type"] == "ivr"

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
    settings.save_extension({"extension": "101", "display_name": "Desk", "active": True}, owner)
    settings.save_number({"number": number, "provider": "TestCarrier", "owner_user_id": owner,
                          "description": "Line", "inbound_extension": "101"})
    node = {"type": "ivr", "prompt": "Hola, marque una extension.", "voice": "es-us",
            "input_timeout": 8, "attempts": 3, "fallback": ""}
    settings.save_call_route(owner, {"phone_number": number, "name": "Main", "route": {"nodes": [node]}, "active": True})
    stored = settings.list_call_routes(owner)[0]["route"]["nodes"][0]
    assert (stored["voice"], stored["input_timeout"], stored["attempts"]) == ("es-us", 8, 3)
    plan = settings.inbound_plan(number, "101")
    assert plan["kind"] == "ivr"
    assert plan["media"] == "sound:custom/ivr-welcome-es-us"
    assert plan["prompt"] == "Hola, marque una extension."
    assert plan["extensions"] == ["101"] and plan["destinations"] == []

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
