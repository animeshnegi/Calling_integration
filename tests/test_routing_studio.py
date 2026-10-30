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

def test_sequential_ring_calls_one_extension_at_a_time_then_runs_voicemail(studio):
    service, asterisk, calls, settings, owner, general, _, general_keys, primary_keys = studio
    first, second = general_keys["101"], general_keys["102"]
    settings.save_call_route(owner, {"phone_number": general, "route": {"nodes": [
        {"type": "sequential", "extensions": [first, second], "timeout": 10, "configured": True},
        {"type": "voicemail", "mailbox": first, "enabled": True, "configured": True},
    ]}})

    call = service.start_inbound(
        {"id": "sequential-call", "caller": {"number": "+13025550000"}},
        general, first,
    )
    assert [extension for _, extension in asterisk.inbound_legs] == [first]
    first_leg = calls.get(call.call_id).employee_channel_id

    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": first_leg}})
    current = calls.get(call.call_id)
    assert [extension for _, extension in asterisk.inbound_legs] == [first, second]
    assert current.extension == second
    second_leg = current.employee_channel_id
    assert second_leg != first_leg

    service.handle_ari_event({"type": "ChannelDestroyed", "channel": {"id": second_leg}})
    assert asterisk.continued == [("sequential-call", "voicemail-inbound", extension_mailbox(first))]


def test_inactive_number_flow_falls_back_without_running_its_ivr(studio):
    _, _, _, settings, owner, general, _, general_keys, _ = studio
    menu = {**settings.default_ivr_node(), "keys": [{"key": "1", "target": general_keys["102"]}]}
    settings.save_call_route(owner, {
        "phone_number": general, "active": False,
        "route": {"nodes": [menu, {"type": "ring_group", "extensions": [general_keys["102"]], "timeout": 25}]},
    })

    plan = settings.inbound_plan(general)
    assert plan.get("kind") != "ivr"
    assert plan["destinations"] == [general_keys["101"]]


def test_overnight_business_hours_are_open_after_midnight_and_before_close(studio, monkeypatch):
    from app import admin as admin_module

    _, _, _, settings, owner, general, _, general_keys, _ = studio

    class FakeDateTime:
        clock = "23:00"
        weekday = 1

        @classmethod
        def now(cls):
            return cls()

        def isoweekday(self):
            return type(self).weekday

        def strftime(self, _format):
            return type(self).clock

    monkeypatch.setattr(admin_module, "datetime", FakeDateTime)
    settings.save_call_route(owner, {"phone_number": general, "route": {"nodes": [
        {"type": "business_hours", "start": "22:00", "end": "06:00", "days": [1]},
        {"type": "ring_group", "extensions": [general_keys["101"]], "timeout": 25},
    ]}})

    FakeDateTime.clock = "23:00"
    FakeDateTime.weekday = 1  # Monday: selected start day
    assert settings.inbound_plan(general)["outside_hours"] is False
    FakeDateTime.clock = "05:00"
    FakeDateTime.weekday = 2  # Tuesday morning still belongs to Monday's window
    assert settings.inbound_plan(general)["outside_hours"] is False
    FakeDateTime.clock = "23:00"
    FakeDateTime.weekday = 2  # Tuesday evening is not selected
    assert settings.inbound_plan(general)["outside_hours"] is True
    FakeDateTime.clock = "12:00"
    assert settings.inbound_plan(general)["outside_hours"] is True


def test_inactive_extension_handling_is_not_applied_to_inbound_calls(studio):
    service, asterisk, _, settings, owner, _, primary, _, primary_keys = studio
    desk = primary_keys["101"]
    settings.save_routing_flow(owner, {
        "target_type": "extension", "target": desk, "active": False,
        "route": {"nodes": [
            {"type": "extension", "extension": desk, "ring_enabled": False,
             "timeout": 15, "no_answer": "hangup", "configured": True},
        ]},
    })

    plan = settings.inbound_plan(primary)
    assert plan["destinations"] == [desk]
    call = service.start_inbound(
        {"id": "inactive-desk-flow", "caller": {"number": "+13025550000"}},
        primary, desk,
    )
    assert [extension for _, extension in asterisk.inbound_legs] == [desk]
    assert call is not None

