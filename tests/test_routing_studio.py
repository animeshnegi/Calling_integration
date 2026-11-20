"""Two focused editors and explicitly shared incoming destinations.

A desk keeps its identity/outbound number. Sharing a DID only changes the
incoming call route, never which customer owns the desk.
"""
import copy

import pytest

from app.admin import SettingsStore, extension_mailbox
from app.telephony_config import TelephonyConfigSync
from test_services import make_service, make_customer, line_setup


@pytest.fixture
def studio(tmp_path):
    service, asterisk, calls = make_service(tmp_path)
    settings = SettingsStore(str(tmp_path / "settings.db"), "secret" * 8)
    service.settings_store = settings
    owner = make_customer(settings, "studio", "Studio Company")
    general, primary = "+13025559001", "+13025559002"
    general_keys = line_setup(settings, general, owner, ("101", "102"))
    primary_keys = line_setup(settings, primary, owner, ("101",))
    return service, asterisk, calls, settings, owner, general, primary, general_keys, primary_keys


def set_handling(settings, owner, key, *, ring=True, action="hangup", extra=(), sources=None):
    data = {"target_type": "extension", "target": key, "route": {"nodes": [
        {"type": "extension", "extension": key, "ring_enabled": ring,
         "timeout": 15, "no_answer": action, "configured": True}, *extra,
    ]}}
    if sources is not None:
        data["ring_sources"] = sources
    return settings.save_routing_flow(owner, data)


def test_shared_and_primary_numbers_ring_the_exact_same_desk(studio):
    service, asterisk, _, settings, owner, general, primary, general_keys, primary_keys = studio
    desk = primary_keys["101"]
    before = settings.extension_row(desk)
    set_handling(settings, owner, desk, sources=[general, primary])
    assert desk in settings.inbound_plan(general)["destinations"]
    assert desk in settings.inbound_plan(primary)["destinations"]
    first = service.start_inbound({"id": "general-call", "caller": {"number": "+13025550000"}}, general, general_keys["101"])
    second = service.start_inbound({"id": "primary-call", "caller": {"number": "+13025550000"}}, primary, desk)
    assert desk in [ext for leg, ext in service._leg_pairs(first)]
    assert desk in [ext for leg, ext in service._leg_pairs(second)]
    after = settings.extension_row(desk)
    for field in ("key", "number", "sip_username", "phone_number_id", "owner_user_id"):
        assert after[field] == before[field]
    assert first.caller_id_number == general
    assert second.caller_id_number == primary


def test_removing_a_ring_source_preserves_other_desks_and_never_resurrects_it(studio):
    _, _, _, settings, owner, general, primary, general_keys, primary_keys = studio
    desk = primary_keys["101"]
    menu = {**settings.default_ivr_node(), "keys": [{"key": "1", "target": general_keys["101"]}]}
    route = {"nodes": [menu, {"type": "ring_group", "extensions": [general_keys["101"], desk], "timeout": 45, "configured": True},
                       {"type": "voicemail", "mailbox": general_keys["101"], "configured": True}]}
    settings.save_call_route(owner, {"phone_number": general, "route": route})
    set_handling(settings, owner, desk, sources=[primary])
    saved = next(flow for flow in settings.list_call_routes(owner) if flow["phone_number"] == general)["route"]
    assert saved["nodes"][0] == menu
    assert saved["nodes"][1]["extensions"] == [general_keys["101"]]
    assert saved["nodes"][1]["timeout"] == 45
    assert saved["nodes"][2] == route["nodes"][2]
    set_handling(settings, owner, desk, sources=[])
    assert settings.inbound_plan(primary)["destinations"] == []


def test_ring_off_goes_straight_to_its_own_mailbox(studio):
    service, asterisk, calls, settings, owner, _, primary, _, primary_keys = studio
    desk = primary_keys["101"]
    set_handling(settings, owner, desk, ring=False, action="voicemail", extra=[
        {"type": "voicemail", "mailbox": desk, "pin": "4321", "enabled": True, "configured": True},
    ])
    call = service.start_inbound({"id": "no-ring", "caller": {"number": "+13025550000"}}, primary, desk)
    assert asterisk.inbound_legs == []
    assert asterisk.continued == [("no-ring", "voicemail-inbound", extension_mailbox(desk))]
    assert "no-ring" not in asterisk.hung_up
    assert calls.get(call.call_id).ended_at


def test_ring_off_runs_external_forward_once_without_affecting_outbound(studio):
    service, asterisk, calls, settings, owner, _, primary, _, primary_keys = studio
    desk = primary_keys["101"]
    set_handling(settings, owner, desk, ring=False, action="forward", extra=[
        {"type": "forward", "phone": "+13025550123", "timeout": 20, "configured": True},
    ])
    service._provider_endpoint = lambda provider: ("PJSIP/test-carrier", {})
    call = service.start_inbound({"id": "forward-only", "caller": {"number": "+13025550000"}}, primary, desk)
    assert asterisk.inbound_legs == []
    assert len(asterisk.forwarded) == 1
    assert asterisk.forwarded[0]["phone"] == "+13025550123"
    assert calls.get(call.call_id).status == "forwarding"
    asterisk.live_channels.clear()
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": calls.get(call.call_id).employee_channel_id}})
    assert len(asterisk.forwarded) == 1
    assert calls.get(call.call_id).ended_at


def test_a_shared_ivr_key_uses_the_selected_desks_ring_switch(studio):
    service, asterisk, _, settings, owner, general, _, general_keys, primary_keys = studio
    desk = primary_keys["101"]
    set_handling(settings, owner, desk, ring=False, action="voicemail", extra=[
        {"type": "voicemail", "mailbox": desk, "enabled": True, "configured": True},
    ])
    menu = {**settings.default_ivr_node(), "keys": [{"key": "1", "target": desk}]}
    settings.save_call_route(owner, {"phone_number": general, "route": {"nodes": [menu]}})
    service.start_inbound({"id": "shared-ivr", "caller": {"number": "+13025550000"}}, general, general_keys["101"])
    service.handle_ari_event({"type": "ChannelDtmfReceived", "channel": {"id": "shared-ivr"}, "digit": "1"})
    assert asterisk.inbound_legs == []
    assert asterisk.continued == [("shared-ivr", "voicemail-inbound", extension_mailbox(desk))]


def test_business_hours_gate_the_ivr_and_route_to_closed_fallback(studio):
    service, asterisk, _, settings, owner, general, _, general_keys, _ = studio
    # An impossible opening interval is closed at every clock time.
    settings.save_call_route(owner, {"phone_number": general, "route": {"nodes": [
        {"type": "business_hours", "start": "23:59", "end": "00:00", "days": [1,2,3,4,5,6,7]},
        settings.default_ivr_node(),
        {"type": "ring_group", "extensions": [general_keys["101"]], "timeout": 25},
        {"type": "voicemail", "mailbox": general_keys["101"]},
    ]}})
    plan = settings.inbound_plan(general)
    assert plan["outside_hours"]
    assert plan["destinations"] == []
    assert plan.get("kind") != "ivr"
    service.start_inbound({"id": "closed-call", "caller": {"number": "+13025550000"}}, general, general_keys["101"])
    assert not service.has_ivr_sessions()
    assert asterisk.inbound_legs == []
    assert asterisk.continued[0][2] == extension_mailbox(general_keys["101"])


def test_foreign_ring_sources_and_ivr_mappings_are_rejected_atomically(studio):
    _, _, _, settings, owner, general, primary, _, primary_keys = studio
    other = make_customer(settings, "other-studio", "Other company")
    foreign_number = "+13025559003"
    foreign = line_setup(settings, foreign_number, other, ("101",))["101"]
    before = copy.deepcopy(settings.list_call_routes())
    before_desk = copy.deepcopy(settings.list_routing_flows(owner))
    with pytest.raises(ValueError, match="owned by this customer"):
        set_handling(settings, owner, primary_keys["101"], sources=[general, foreign_number])
    assert settings.list_call_routes() == before
    assert settings.list_routing_flows(owner) == before_desk
    with pytest.raises(ValueError, match="owned by this customer"):
        settings.save_call_route(owner, {"phone_number": general, "route": {"nodes": [
            {**settings.default_ivr_node(), "keys": [{"key": "1", "target": foreign}]},
        ]}})
    with pytest.raises(ValueError, match="owned by this customer"):
        settings.save_call_route(owner, {"phone_number": primary, "route": {"nodes": [
            {"type": "extension", "extension": foreign},
        ]}})


def test_general_number_with_no_primary_desk_has_an_explicit_shared_anchor(studio, tmp_path):
    service, asterisk, _, settings, owner, _, primary, _, primary_keys = studio
    shared = "+13025559009"
    settings.save_number({"number": shared, "description": "Company switchboard", "provider": "TestCarrier", "owner_user_id": owner, "active": True})
    desk = primary_keys["101"]
    assert settings.number_flow_anchor(shared) == ""
    set_handling(settings, owner, desk, sources=[shared, primary])
    assert settings.number_flow_anchor(shared) == desk
    dialplan = TelephonyConfigSync(settings, None, str(tmp_path / "dynamic.conf")).render_dialplan()
    assert f"Stasis(engineerip,inbound,{shared},{desk})" in dialplan
    call = service.start_inbound({"id": "switchboard", "caller": {"number": "+13025550000"}}, shared, desk)
    assert call is not None
    assert [ext for leg, ext in service._leg_pairs(call)] == [desk]
    assert call.caller_id_number == shared
    assert call.provider == "TestCarrier"


@pytest.mark.parametrize("admin_side", [False, True])
def test_both_roles_save_shared_sources_through_the_real_endpoint(tmp_path, monkeypatch, admin_side):
    from test_provisioning import make_app, customer_client, admin_client
    import app as app_module

    applied = []
    monkeypatch.setattr(app_module.TelephonyConfigSync, "apply", lambda self: applied.append(True))
    app = make_app(tmp_path)
    customer, owner = customer_client(app, "studio-api")
    store = app.extensions["settings_store"]
    general, primary = "+13025559101", "+13025559102"
    store.save_provider({"name": "TestCarrier", "server": "sip.example.com", "port": 5060,
                         "username": "test", "password": "test-password", "transport": "udp",
                         "codecs": "ulaw,alaw", "allowed_ips": "198.51.100.10/32"})
    line_setup(store, general, owner, ("101",))
    desk = line_setup(store, primary, owner, ("101",))["101"]
    client = admin_client(app) if admin_side else customer
    before = store.extension_row(desk)
    response = client.post("/admin/api/call-routes", json={
        "owner_user_id": owner, "target_type": "extension", "target": desk,
        "ring_sources": [general, primary],
        "route": {"nodes": [{"type": "extension", "extension": desk,
                                "ring_enabled": True, "timeout": 15, "no_answer": "hangup", "configured": True}]},
    })
    assert response.status_code == 200, response.json
    assert response.json["warning"] == ""
    assert applied
    assert desk in store.inbound_plan(general)["destinations"]
    assert store.extension_row(desk)["sip_username"] == before["sip_username"]
    assert store.extension_row(desk)["number"] == primary
    # A route target and sources on another account still cannot be written by a customer.
    other_client, other = customer_client(app, "studio-api-other")
    response = other_client.post("/admin/api/call-routes", json={
        "owner_user_id": owner, "target_type": "extension", "target": desk,
        "ring_sources": [general], "route": {"nodes": []},
    })
    assert response.status_code in (400, 403, 404)


def test_explicit_end_call_overrides_the_old_mailbox_switch(studio):
    _, _, _, settings, owner, _, primary, _, primary_keys = studio
    key = primary_keys["101"]
    settings.save_extension({"extension": key, "voicemail_enabled": True, "voicemail_pin": "4321"}, owner)
    set_handling(settings, owner, key, action="hangup")
    assert settings.inbound_plan(primary)["voicemail"] == ""


def test_ring_then_external_forward_uses_the_desk_policy(studio):
    service, asterisk, calls, settings, owner, _, primary, _, primary_keys = studio
    desk = primary_keys["101"]
    set_handling(settings, owner, desk, action="forward", extra=[
        {"type": "forward", "phone": "+13025550123", "timeout": 20, "configured": True},
    ])
    service._provider_endpoint = lambda provider: ("PJSIP/test-carrier", {})
    call = service.start_inbound({"id": "ring-then-forward", "caller": {"number": "+13025550000"}}, primary, desk)
    assert asterisk.inbound_timeouts[0]["timeout"] == 15
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": call.employee_channel_id}})
    assert asterisk.forwarded[0]["phone"] == "+13025550123"
    assert calls.get(call.call_id).status == "forwarding"


def test_ambiguous_desk_policy_is_rejected(studio):
    _, _, _, settings, owner, _, _, _, keys = studio
    key = keys["101"]
    with pytest.raises(ValueError, match="exactly one"):
        set_handling(settings, owner, key, action="voicemail", extra=[
            {"type": "voicemail", "mailbox": key, "enabled": True},
            {"type": "forward", "phone": "+13025550123", "timeout": 25},
        ])


def test_forwarded_to_desk_with_ringing_off_takes_its_message_without_a_loop(studio):
    service, asterisk, calls, settings, owner, general, _, general_keys, primary_keys = studio
    first, target = general_keys["101"], primary_keys["101"]
    settings.save_call_route(owner, {"phone_number": general, "route": {"nodes": [
        {"type": "extension", "extension": first, "timeout": 15},
    ]}})
    set_handling(settings, owner, first, action="extension", extra=[
        {"type": "extension", "extension": target, "timeout": 15},
    ])
    set_handling(settings, owner, target, ring=False, action="voicemail", extra=[
        {"type": "voicemail", "mailbox": target, "enabled": True},
    ])
    call = service.start_inbound({"id": "internal-forward", "caller": {"number": "+13025550000"}}, general, first)
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": call.employee_channel_id}})
    assert [extension for _, extension in asterisk.inbound_legs] == [first]
    assert asterisk.continued == [("internal-forward", "voicemail-inbound", extension_mailbox(target))]
    assert "internal-forward" not in asterisk.hung_up
    assert calls.get(call.call_id).ended_at


@pytest.mark.parametrize("mode,initial", [("sequential", 1), ("simultaneous", 2), ("ring_group", 2)])
def test_ring_mode_controls_which_desks_ring(studio, mode, initial):
    service, asterisk, calls, settings, owner, number, _, keys, _ = studio
    members = [keys["101"], keys["102"]]
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": mode, "extensions": members, "timeout": 25},
        {"type": "voicemail", "mailbox": members[0]},
    ]}})
    call = service.start_inbound({"id": "mode-call", "caller": {"number": "+13025550000"}}, number, members[0])
    assert [extension for _, extension in asterisk.inbound_legs] == members[:initial]
    if mode == "sequential":
        first_leg = call.employee_channel_id
        service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": first_leg}})
        assert [extension for _, extension in asterisk.inbound_legs] == members
        assert not asterisk.continued
        second_leg = calls.get(call.call_id).employee_channel_id
        assert second_leg != first_leg
        # A duplicate old event cannot skip the second extension.
        service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": first_leg}})
        assert not asterisk.continued
        service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": second_leg}})
        assert asterisk.continued == [("mode-call", "voicemail-inbound", extension_mailbox(members[0]))]
    assert all(item["timeout"] == 25 for item in asterisk.inbound_timeouts)


def test_ivr_selected_desk_uses_own_voicemail_not_number_fallback(studio):
    service, asterisk, calls, settings, owner, number, _, keys, _ = studio
    first, selected = keys["101"], keys["102"]
    set_handling(settings, owner, selected, action="voicemail", extra=[
        {"type": "voicemail", "mailbox": selected, "enabled": True},
    ])
    menu = {**settings.default_ivr_node(), "keys": [{"key": "2", "target": selected}]}
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        menu, {"type": "voicemail", "mailbox": first},
    ]}})
    call = service.start_inbound({"id": "select-second", "caller": {"number": "+13025550000"}}, number, first)
    assert not asterisk.inbound_legs
    service.handle_ari_event({"type": "ChannelDtmfReceived", "channel": {"id": "select-second"}, "digit": "2"})
    current = calls.get(call.call_id)
    assert [ext for _, ext in asterisk.inbound_legs] == [selected]
    assert asterisk.inbound_timeouts[-1]["timeout"] == 15
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": current.employee_channel_id}})
    assert asterisk.continued == [("select-second", "voicemail-inbound", extension_mailbox(selected))]
    assert "routing_plan" not in calls.get(call.call_id).to_dict()


@pytest.mark.parametrize("number_timeout,desk_timeout,expected", [(25, 60, 25), (60, 25, 25), (10, 15, 10)])
def test_number_and_desk_timers_use_shorter_limit(studio, number_timeout, desk_timeout, expected):
    service, asterisk, _, settings, owner, number, _, keys, _ = studio
    key = keys["101"]
    settings.save_routing_flow(owner, {"target_type": "extension", "target": key, "route": {"nodes": [
        {"type": "extension", "extension": key, "timeout": desk_timeout},
    ]}})
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": "ring_group", "extensions": [key], "timeout": number_timeout},
    ]}})
    service.start_inbound({"id": "timed", "caller": {"number": "+13025550000"}}, number, key)
    assert asterisk.inbound_timeouts[-1]["timeout"] == expected


def test_ordered_extension_steps_survive_a_worker_restart_and_flow_edit(studio):
    from app.services import TelephonyService
    service, asterisk, calls, settings, owner, number, _, keys, _ = studio
    first, second = keys["101"], keys["102"]
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": "extension", "extension": first, "timeout": 10},
        {"type": "extension", "extension": second, "timeout": 20},
        {"type": "voicemail", "mailbox": first},
    ]}})
    call = service.start_inbound({"id": "restart-stages", "caller": {"number": "+13025550000"}}, number, first)
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": "voicemail", "mailbox": second},
    ]}})
    restarted = TelephonyService(asterisk, service.config, store=calls, settings_store=settings)
    restarted.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": call.employee_channel_id}})
    assert [extension for _, extension in asterisk.inbound_legs] == [first, second]
    assert [item["timeout"] for item in asterisk.inbound_timeouts] == [10, 20]
    restarted.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": calls.get(call.call_id).employee_channel_id}})
    assert asterisk.continued == [("restart-stages", "voicemail-inbound", extension_mailbox(first))]


def test_all_unanswered_desks_use_first_desks_mailbox(studio):
    service, asterisk, _, settings, owner, number, _, keys, _ = studio
    for key in keys.values():
        set_handling(settings, owner, key, action="voicemail", extra=[
            {"type": "voicemail", "mailbox": key, "enabled": True},
        ])
    first, second = keys["101"], keys["102"]
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": "simultaneous", "extensions": [first, second], "timeout": 25},
    ]}})
    call = service.start_inbound({"id": "first-mailbox", "caller": {"number": "+13025550000"}}, number, first)
    for leg, _ in service._leg_pairs(call):
        service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": leg}})
    assert asterisk.continued == [("first-mailbox", "voicemail-inbound", extension_mailbox(first))]


def test_business_hours_gate_even_when_tile_follows_ring(studio, monkeypatch):
    from datetime import datetime
    import app.admin as admin
    class ClosedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 23, 20, 0)  # Monday, after closing.
    monkeypatch.setattr(admin, "datetime", ClosedClock)
    service, asterisk, _, settings, owner, number, _, keys, _ = studio
    first = keys["101"]
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": "simultaneous", "extensions": list(keys.values()), "timeout": 25},
        {"type": "business_hours", "start": "09:00", "end": "17:00", "days": [1]},
        {"type": "voicemail", "mailbox": first},
    ]}})
    service.start_inbound({"id": "hours-order", "caller": {"number": "+13025550000"}}, number, first)
    assert not asterisk.inbound_legs
    assert asterisk.continued == [("hours-order", "voicemail-inbound", extension_mailbox(first))]


def test_overnight_business_hours_use_opening_weekday(studio, monkeypatch):
    from datetime import datetime
    import app.admin as admin
    class OvernightClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 24, 1, 0)  # Tuesday belongs to Monday's shift.
    monkeypatch.setattr(admin, "datetime", OvernightClock)
    _, _, _, settings, owner, number, _, keys, _ = studio
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": "business_hours", "start": "22:00", "end": "06:00", "days": [1]},
        {"type": "extension", "extension": keys["101"], "timeout": 25},
    ]}})
    assert settings.inbound_plan(number)["destinations"] == [keys["101"]]
    with pytest.raises(ValueError, match="opening and closing"):
        settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
            {"type": "business_hours", "start": "29:00", "end": "06:00", "days": [1]},
        ]}})


def test_ivr_waits_for_prompt_completion_before_input_timeout(studio, monkeypatch):
    import app.services as engine
    now = [1000.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: now[0])
    service, asterisk, _, settings, owner, number, _, keys, _ = studio
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [settings.default_ivr_node()]}})
    service.start_inbound({"id": "long-prompt", "caller": {"number": "+13025550000"}}, number, keys["101"])
    session = service._ivr_sessions_map()["long-prompt"]
    assert service.process_ivr_timeouts(now=1007) == 0
    now[0] = 1010
    service.handle_ari_event({"type": "PlaybackFinished", "playback": {"id": session["playback_id"]}})
    assert session["deadline"] == 1016
    assert service.process_ivr_timeouts(now=1015) == 0
    assert service.process_ivr_timeouts(now=1016) == 1
    assert session["tries"] == 1


def test_disabled_ivr_and_inactive_desk_policy_do_not_interfere(studio):
    service, asterisk, _, settings, owner, number, _, keys, _ = studio
    first = keys["101"]
    settings.save_routing_flow(owner, {"target_type": "extension", "target": first, "active": False, "route": {"nodes": [
        {"type": "extension", "extension": first, "timeout": 5, "ring_enabled": False},
    ]}})
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {**settings.default_ivr_node(), "enabled": False},
        {"type": "extension", "extension": first, "timeout": 25},
    ]}})
    service.start_inbound({"id": "disabled-policy", "caller": {"number": "+13025550000"}}, number, first)
    assert not service.has_ivr_sessions()
    assert [extension for _, extension in asterisk.inbound_legs] == [first]
    assert asterisk.inbound_timeouts[-1]["timeout"] == 25


def test_workflow_voicemail_is_provisioned_even_without_extension_switch(studio):
    _, _, _, settings, owner, number, _, keys, _ = studio
    first, selected = keys["101"], keys["102"]
    assert not settings.extension_voicemail_enabled(selected)
    set_handling(settings, owner, selected, action="voicemail", extra=[
        {"type": "voicemail", "mailbox": selected, "enabled": True, "pin": "4321"},
    ])
    sync = TelephonyConfigSync(settings, None, "/tmp/unused-workflow-render.conf")
    assert f"{extension_mailbox(selected)} =>" in sync.render_voicemail()
    assert f"exten => {extension_mailbox(selected)},1,VoiceMail({extension_mailbox(selected)}@engineerip,u)" in sync.render_dialplan()


def test_ivr_selected_forward_policy_does_not_inherit_number_voicemail(studio):
    service, asterisk, calls, settings, owner, number, _, keys, _ = studio
    first, selected = keys["101"], keys["102"]
    set_handling(settings, owner, selected, action="forward", extra=[
        {"type": "forward", "phone": "+13025550123", "timeout": 20},
    ])
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {**settings.default_ivr_node(), "keys": [{"key": "2", "target": selected}]},
        {"type": "voicemail", "mailbox": first},
    ]}})
    call = service.start_inbound({"id": "ivr-forward", "caller": {"number": "+13025550000"}}, number, first)
    service.handle_ari_event({"type": "ChannelDtmfReceived", "channel": {"id": "ivr-forward"}, "digit": "2"})
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": calls.get(call.call_id).employee_channel_id}})
    assert not asterisk.continued
    assert asterisk.forwarded == [{"phone": "+13025550123", "timeout": 20, "caller_id": number}]


def test_after_hours_forward_uses_configured_forward_timer(studio, monkeypatch):
    from datetime import datetime
    import app.admin as admin
    class ClosedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 23, 20, 0)
    monkeypatch.setattr(admin, "datetime", ClosedClock)
    service, asterisk, _, settings, owner, number, _, keys, _ = studio
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": "business_hours", "start": "09:00", "end": "17:00", "days": [1]},
        {"type": "extension", "extension": keys["101"], "timeout": 25},
        {"type": "forward", "phone": "+13025550123", "timeout": 40},
    ]}})
    service.start_inbound({"id": "closed-forward", "caller": {"number": "+13025550000"}}, number, keys["101"])
    assert not asterisk.inbound_legs
    assert asterisk.forwarded[-1]["timeout"] == 40


@pytest.mark.parametrize("input_timeout", [2, 8, 30])
def test_ivr_uses_configured_wait_between_digits_and_ignores_old_playback(studio, monkeypatch, input_timeout):
    import app.services as engine
    clock = [1000.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    service, asterisk, _, settings, owner, number, _, keys, _ = studio
    menu = {**settings.default_ivr_node(), "input_timeout": input_timeout, "attempts": 2}
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [menu]}})
    service.start_inbound({"id": "digit-clock", "caller": {"number": "+13025550000"}}, number, keys["101"])
    session = service._ivr_sessions_map()["digit-clock"]
    playback_id = session["playback_id"]
    service.handle_ari_event({"type": "ChannelDtmfReceived", "channel": {"id": "digit-clock"}, "digit": "1"})
    assert playback_id in asterisk.stopped_playbacks
    assert session["deadline"] == 1000 + input_timeout
    assert service.process_ivr_timeouts(now=1000 + input_timeout - .1) == 0
    # The next digit gets a full configured interval, not a hard-coded 1.4s.
    clock[0] = 1000 + input_timeout - .1
    service.handle_ari_event({"type": "ChannelDtmfReceived", "channel": {"id": "digit-clock"}, "digit": "0"})
    expected_deadline = clock[0] + input_timeout
    clock[0] += .05
    service.handle_ari_event({"type": "PlaybackFinished", "playback": {"id": playback_id}})
    assert session["deadline"] == expected_deadline
    assert service.process_ivr_timeouts(now=expected_deadline - .01) == 0
    assert service.process_ivr_timeouts(now=expected_deadline) == 1
    assert session["tries"] == 1
    assert session["digits"] == ""
    assert len(asterisk.played) == 2


@pytest.mark.parametrize("forward_timeout,destination_timeout,expected", [(45, 90, 45), (90, 45, 45), (5, 90, 5)])
def test_forwarded_desk_uses_new_step_timer_not_original_number_limit(studio, forward_timeout, destination_timeout, expected):
    service, asterisk, calls, settings, owner, number, _, keys, _ = studio
    first, second = keys["101"], keys["102"]
    settings.save_routing_flow(owner, {"target_type": "extension", "target": first, "route": {"nodes": [
        {"type": "extension", "extension": first, "timeout": 60, "no_answer": "extension"},
        {"type": "extension", "extension": second, "timeout": forward_timeout},
    ]}})
    settings.save_routing_flow(owner, {"target_type": "extension", "target": second, "route": {"nodes": [
        {"type": "extension", "extension": second, "timeout": destination_timeout, "no_answer": "hangup"},
    ]}})
    settings.save_call_route(owner, {"phone_number": number, "route": {"nodes": [
        {"type": "ring_group", "extensions": [first], "timeout": 10},
    ]}})
    call = service.start_inbound({"id": "forward-clock", "caller": {"number": "+13025550000"}}, number, first)
    assert asterisk.inbound_timeouts[-1]["timeout"] == 10
    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": call.employee_channel_id}})
    assert calls.get(call.call_id).status == "ringing_fallback"
    assert asterisk.inbound_timeouts[-1]["extension"] == second
    assert asterisk.inbound_timeouts[-1]["timeout"] == expected


def test_workflow_pin_replaces_existing_pin_and_blank_save_preserves_it(studio, tmp_path):
    _, _, _, settings, owner, number, _, keys, _ = studio
    key = keys['101']
    settings.save_extension({'extension': key, 'voicemail_enabled': True, 'voicemail_pin': '1111'}, owner)
    route = {'nodes': [{'type': 'extension', 'extension': key, 'timeout': 25},
                       {'type': 'voicemail', 'mailbox': key, 'enabled': True, 'pin': '8765432109'}]}
    settings.save_routing_flow(owner, {'target_type': 'extension', 'target': key, 'route': route})
    assert settings.get_voicemail_pin(key) == '8765432109'
    sync = TelephonyConfigSync(settings, object(), str(tmp_path / 'pjsip.dynamic.conf'))
    assert f'{extension_mailbox(key)} => 8765432109,' in sync.render_voicemail()
    # No plaintext secret is retained in route JSON or returned to the console.
    saved = next(flow for flow in settings.list_routing_flows(owner) if flow['target'] == key)
    assert 'pin' not in saved['route']['nodes'][-1]
    settings.save_routing_flow(owner, {'target_type': 'extension', 'target': key,
                                      'route': {'nodes': [{'type': 'voicemail', 'mailbox': key, 'pin': ''}]}})
    assert settings.get_voicemail_pin(key) == '8765432109'
    settings.save_call_route(owner, {'phone_number': number, 'route': {'nodes': [
        {'type': 'voicemail', 'mailbox': key, 'pin': '2468'},
    ]}})
    assert settings.get_voicemail_pin(key) == '2468'
    assert f'{extension_mailbox(key)} => 2468,' in sync.render_voicemail()


@pytest.mark.parametrize('pin', ['123', '12345678901', '12x45', '1234;bad'])
def test_invalid_workflow_pin_does_not_change_mailbox(studio, pin):
    _, _, _, settings, owner, _, _, keys, _ = studio
    key = keys['101']
    settings.save_extension({'extension': key, 'voicemail_enabled': True, 'voicemail_pin': '4321'}, owner)
    with pytest.raises(ValueError, match='PIN'):
        settings.save_routing_flow(owner, {'target_type': 'extension', 'target': key, 'route': {'nodes': [
            {'type': 'voicemail', 'mailbox': key, 'pin': pin},
        ]}})
    assert settings.get_voicemail_pin(key) == '4321'


def test_physical_and_browser_endpoints_bind_own_mailbox_and_advertise_access_code(studio, tmp_path):
    import re
    _, _, _, settings, owner, _, _, keys, primary_keys = studio
    # The same digits on two numbers must never send the user to a generic
    # mailbox-number prompt: a hyphenated scoped mailbox cannot be typed as DTMF.
    for key in (keys['101'], primary_keys['101']):
        settings.save_extension({'extension': key, 'webrtc_enabled': True}, owner)
        settings.save_routing_flow(owner, {'target_type': 'extension', 'target': key, 'route': {'nodes': [
            {'type': 'voicemail', 'mailbox': key, 'pin': '5678', 'enabled': True},
        ]}})
    sync = TelephonyConfigSync(settings, object(), str(tmp_path / 'pjsip.dynamic.conf'))
    pjsip = sync.render_pjsip(media_address='192.0.2.1')
    endpoints = re.findall(r'\[[^\]]+\]\ntype=endpoint\n(.*?)(?=\n\[|\Z)', pjsip, re.S)
    for key in (keys['101'], primary_keys['101']):
        mailbox = extension_mailbox(key)
        matching = [body for body in endpoints if f'set_var=ENGINEERIP_MAILBOX={mailbox}\n' in body]
        assert len(matching) >= 3  # canonical endpoint, SIP username alias, browser
        for body in matching:
            assert 'voicemail_extension=*97' in body
            assert f'mailboxes={mailbox}@engineerip' in body
    dialplan = sync.render_dialplan()
    assert 'VoiceMailMain(${ENGINEERIP_MAILBOX}@engineerip)' in dialplan
    assert 'VoiceMailMain(@engineerip)' not in dialplan
    assert 'GotoIf($["${ENGINEERIP_MAILBOX}"=""]?unavailable)' in dialplan
    assert 'VoiceMailMain(${ENGINEERIP_MAILBOX}@engineerip,s)' not in dialplan  # PIN is never bypassed
