import base64

import pytest

from app.asterisk_client import AsteriskError, AsteriskClient


class Response:
    ok = True
    content = b'{"id":"ok"}'

    def json(self):
        return {"id": "ok"}


def test_stored_recording_file_uses_private_ari_and_range(monkeypatch):
    captured = {}

    class StreamResponse:
        status_code = 206
        headers = {"Content-Type": "audio/wav"}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return StreamResponse()

    monkeypatch.setattr("app.asterisk_client.requests.get", fake_get)
    client = AsteriskClient(type("Config", (), {
        "ASTERISK_ARI_URL": "http://asterisk:8088/ari",
        "ASTERISK_ARI_USER": "u", "ASTERISK_ARI_PASSWORD": "p", "ASTERISK_ARI_APP": "app",
    }))
    response = client.open_stored_recording("call-123", "bytes=10-")
    assert response.status_code == 206
    assert captured["url"].endswith("/recordings/stored/call-123/file")
    assert captured["headers"] == {"Range": "bytes=10-"}
    assert captured["auth"] == ("u", "p")


def test_originate_sends_variables_as_json_body(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.asterisk_client.requests.request", fake_request)
    client = AsteriskClient(type("Config", (), {
        "ASTERISK_ARI_URL": "http://asterisk:8088/ari",
        "ASTERISK_ARI_USER": "u",
        "ASTERISK_ARI_PASSWORD": "p",
        "ASTERISK_ARI_APP": "engineerip",
    }))
    client.create_outbound_call("call-1", "101", "+13025551234", "provider-1", {"contact_id": "42"})

    assert captured["json"]["variables"]["EIP_CALL_ID"] == "call-1"
    assert captured["json"]["variables"]["EIP_CONTACT_ID"] == "42"
    assert "variables" not in captured["params"]


def test_customer_leg_uses_assigned_callback_number(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.asterisk_client.requests.request", fake_request)
    client = AsteriskClient(type("Config", (), {
        "ASTERISK_ARI_URL": "http://asterisk:8088/ari", "ASTERISK_ARI_USER": "u",
        "ASTERISK_ARI_PASSWORD": "p", "ASTERISK_ARI_APP": "engineerip",
    }))
    client.create_customer_leg("call-1", "+919999999999", "provider-1", "employee-1", "+13025550101")
    assert captured["params"]["callerId"] == "+13025550101"


def ari_client(**overrides):
    """Build an AsteriskClient from an ad-hoc config without touching the network."""
    values = {
        "ASTERISK_ARI_URL": "http://asterisk:8088/ari",
        "ASTERISK_ARI_USER": "u",
        "ASTERISK_ARI_PASSWORD": "p",
        "ASTERISK_ARI_APP": "engineerip",
        "ASTERISK_RECORDING_PATH": "/var/spool/asterisk/recording",
    }
    values.update(overrides)
    return AsteriskClient(type("Config", (), values))


def capture_request(monkeypatch):
    """Record the next requests.request call and answer it with a canned JSON body."""
    captured = {}

    class Ok:
        ok = True
        status_code = 200
        content = b'{"id":"ok"}'

        def json(self):
            return {"id": "ok"}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return Ok()

    monkeypatch.setattr("app.asterisk_client.requests.request", fake_request)
    return captured


def test_request_always_sends_ari_basic_auth_and_timeout(monkeypatch):
    captured = capture_request(monkeypatch)

    ari_client().list_channels()

    assert captured["auth"] == ("u", "p")
    assert captured["timeout"] == 10


def test_trailing_slash_in_ari_url_is_normalised(monkeypatch):
    captured = capture_request(monkeypatch)

    ari_client(ASTERISK_ARI_URL="http://asterisk:8088/ari/").list_channels()

    assert captured["url"] == "http://asterisk:8088/ari/channels"


def test_request_raises_asterisk_error_carrying_the_status_code(monkeypatch):
    class Failing:
        ok = False
        status_code = 503
        content = b'{"message":"unavailable"}'

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *args, **kwargs: Failing())

    with pytest.raises(AsteriskError) as excinfo:
        ari_client().health()

    assert "503" in str(excinfo.value)


def test_request_returns_none_for_an_empty_response_body(monkeypatch):
    class Empty:
        ok = True
        status_code = 204
        content = b""

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *args, **kwargs: Empty())

    assert ari_client().hangup("channel-1") is None


def test_channel_and_recording_listings_are_always_lists(monkeypatch):
    class Payload:
        ok = True
        status_code = 200
        content = b'{"unexpected":true}'

        def json(self):
            return {"unexpected": True}

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *args, **kwargs: Payload())

    client = ari_client()
    assert client.list_channels() == []
    assert client.list_stored_recordings() == []


def test_endpoint_listing_falls_back_to_an_empty_list_for_an_empty_body(monkeypatch):
    class Empty:
        ok = True
        status_code = 204
        content = b""

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *args, **kwargs: Empty())

    assert ari_client().list_endpoints() == []


def test_event_url_and_headers_use_websocket_scheme_and_basic_auth():
    client = ari_client()

    assert client.event_url() == "ws://asterisk:8088/ari/events?app=engineerip"
    expected = base64.b64encode(b"u:p").decode()
    assert client.event_headers() == [f"Authorization: Basic {expected}"]


def test_event_url_upgrades_to_wss_for_tls_ari():
    client = ari_client(ASTERISK_ARI_URL="https://asterisk:8089/ari", ASTERISK_ARI_APP="app2")

    assert client.event_url() == "wss://asterisk:8089/ari/events?app=app2"


def test_outbound_call_originates_employee_first_with_deterministic_channel(monkeypatch):
    captured = capture_request(monkeypatch)

    assert ari_client().create_outbound_call("call-1", "101", "+13025551234", "provider-1", None) == "call-1"

    assert captured["params"]["endpoint"] == "PJSIP/101"
    assert captured["params"]["channelId"] == "call-1-employee"
    assert captured["params"]["timeout"] == 30
    assert captured["params"]["appArgs"] == "employee,call-1,provider-1,+13025551234"
    assert captured["json"]["variables"] == {"EIP_CALL_ID": "call-1"}


def test_outbound_call_variables_omit_absent_metadata(monkeypatch):
    captured = capture_request(monkeypatch)

    ari_client().create_outbound_call(
        "call-1", "101", "+13025551234", "provider-1", {"contact_id": None, "member_id": 7}
    )

    assert captured["json"]["variables"] == {"EIP_CALL_ID": "call-1", "EIP_MEMBER_ID": "7"}


def test_inbound_employee_leg_is_originated_from_the_customer_channel(monkeypatch):
    captured = capture_request(monkeypatch)

    assert ari_client().create_inbound_employee_leg("call-1", "102", "customer-1") == "call-1-employee"

    assert captured["params"]["endpoint"] == "PJSIP/102"
    assert captured["params"]["originator"] == "customer-1"
    assert captured["params"]["appArgs"] == "inbound_employee,call-1"
    assert captured["params"]["channelId"] == "call-1-employee"


def test_customer_leg_targets_the_provider_endpoint_from_the_employee_channel(monkeypatch):
    captured = capture_request(monkeypatch)

    channel = ari_client().create_customer_leg("call-1", "+13025551234", "provider-1", "call-1-employee", None)

    assert channel == "call-1-customer"
    assert captured["params"]["endpoint"] == "PJSIP/+13025551234@provider-1"
    assert captured["params"]["originator"] == "call-1-employee"
    assert captured["params"]["callerId"] == ""
    assert captured["params"]["timeout"] == 60


def test_create_bridge_uses_mixing_type_and_deterministic_id(monkeypatch):
    captured = capture_request(monkeypatch)

    assert ari_client().create_bridge("call-1") == "bridge-call-1"

    assert captured["params"] == {
        "type": "mixing",
        "bridgeId": "bridge-call-1",
        "name": "EngineerIP call-1",
    }


def test_add_channel_to_bridge_posts_the_channel_parameter(monkeypatch):
    captured = capture_request(monkeypatch)

    ari_client().add_channel_to_bridge("bridge-1", "channel-1")

    assert captured["url"].endswith("/bridges/bridge-1/addChannel")
    assert captured["params"] == {"channel": "channel-1"}


def test_start_bridge_recording_fails_on_conflict_and_never_terminates_on(monkeypatch):
    captured = capture_request(monkeypatch)

    ari_client().start_bridge_recording("bridge-1", "call-1", "wav", True, 120)

    assert captured["params"] == {
        "name": "call-1",
        "format": "wav",
        "ifExists": "fail",
        "beep": "true",
        "maxDurationSeconds": 120,
        "terminateOn": "none",
    }


def test_bridge_recording_beep_flag_is_stringified(monkeypatch):
    captured = capture_request(monkeypatch)

    ari_client().start_bridge_recording("bridge-1", "call-1", "gsm", False, 0)

    assert captured["params"]["beep"] == "false"
    assert captured["params"]["maxDurationSeconds"] == 0


@pytest.mark.parametrize("media", ["", "x" * 257, "sound:hello\r\ninjected", "tones:1"])
def test_play_bridge_media_rejects_unsafe_or_unsupported_media(monkeypatch, media):
    captured = capture_request(monkeypatch)

    with pytest.raises(ValueError):
        ari_client().play_bridge_media("bridge-1", media)

    assert captured == {}


@pytest.mark.parametrize("media", ["sound:ai-welcome", "recording:call-123"])
def test_play_bridge_media_forwards_allowed_media_uri(monkeypatch, media):
    captured = capture_request(monkeypatch)

    ari_client().play_bridge_media("bridge-1", media)

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/bridges/bridge-1/play")
    assert captured["params"] == {"media": media}


def test_continue_in_dialplan_targets_context_extension_and_priority(monkeypatch):
    captured = capture_request(monkeypatch)

    ari_client().continue_in_dialplan("channel-1", "voicemail-inbound", "101")

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/channels/channel-1/continue")
    assert captured["params"] == {"context": "voicemail-inbound", "extension": "101", "priority": 1}


def test_health_reads_asterisk_info(monkeypatch):
    captured = capture_request(monkeypatch)

    assert ari_client().health() == {"id": "ok"}

    assert captured["method"] == "GET"
    assert captured["url"].endswith("/asterisk/info")


def test_deprecated_cleanup_helper_never_deletes(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("age-based cleanup must not touch ARI")

    monkeypatch.setattr("app.asterisk_client.requests.request", fail)

    # Retention is enforced by TelephonyService from persisted call ended_at values.
    assert ari_client().cleanup_old_recordings(30) == 0


# The guard rejects anything that could introduce a path separator or header
# break; a bare ".." cannot traverse without one, so it is intentionally absent.
UNSAFE_RECORDING_NAMES = ["", "../etc/passwd", "a/b", "a\\b", "line\nbreak", "carriage\rreturn", "x" * 129]


@pytest.mark.parametrize("name", UNSAFE_RECORDING_NAMES)
def test_recording_helpers_reject_unsafe_names(monkeypatch, name):
    def fail(*args, **kwargs):
        raise AssertionError("no request may be made for an unsafe recording name")

    monkeypatch.setattr("app.asterisk_client.requests.request", fail)
    monkeypatch.setattr("app.asterisk_client.requests.get", fail)

    client = ari_client()
    assert client.get_stored_recording(name) is None
    assert client.delete_stored_recording(name) is False
    assert client.open_stored_recording(name) is None


def test_stored_recording_metadata_derives_filename_from_ari_format(monkeypatch):
    class Payload:
        ok = True
        status_code = 200
        content = b'{"name":"call-1","format":"wav"}'

        def json(self):
            return {"name": "call-1", "format": "wav"}

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *args, **kwargs: Payload())

    recording = ari_client().get_stored_recording("call-1")

    assert recording["filename"] == "/var/spool/asterisk/recording/call-1.wav"


def test_stored_recording_metadata_keeps_an_existing_filename(monkeypatch):
    class Payload:
        ok = True
        status_code = 200
        content = b'{"name":"call-1","format":"wav","filename":"/custom/call-1.wav"}'

        def json(self):
            return {"name": "call-1", "format": "wav", "filename": "/custom/call-1.wav"}

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *args, **kwargs: Payload())

    assert ari_client().get_stored_recording("call-1")["filename"] == "/custom/call-1.wav"


def test_stored_recording_metadata_is_none_when_asterisk_errors(monkeypatch):
    class Failing:
        ok = False
        status_code = 404
        content = b""

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *args, **kwargs: Failing())

    assert ari_client().get_stored_recording("call-1") is None


def test_stored_recording_metadata_is_none_for_a_non_object_payload(monkeypatch):
    class Payload:
        ok = True
        status_code = 200
        content = b"[]"

        def json(self):
            return []

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *args, **kwargs: Payload())

    assert ari_client().get_stored_recording("call-1") is None


def test_delete_stored_recording_reports_true_on_success(monkeypatch):
    captured = capture_request(monkeypatch)

    assert ari_client().delete_stored_recording("call-1") is True

    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/recordings/stored/call-1")


def test_delete_stored_recording_reports_false_on_asterisk_error(monkeypatch):
    class Failing:
        ok = False
        status_code = 500
        content = b""

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *args, **kwargs: Failing())

    assert ari_client().delete_stored_recording("call-1") is False


def test_open_stored_recording_closes_the_stream_when_asterisk_rejects_it(monkeypatch):
    closed = []

    class NotFound:
        status_code = 404
        headers = {}

        def close(self):
            closed.append(True)

    monkeypatch.setattr("app.asterisk_client.requests.get", lambda *args, **kwargs: NotFound())

    assert ari_client().open_stored_recording("call-1") is None
    assert closed == [True]


def test_open_stored_recording_ignores_a_malformed_range_header(monkeypatch):
    captured = {}

    class Ok:
        status_code = 200
        headers = {"Content-Type": "audio/wav"}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return Ok()

    monkeypatch.setattr("app.asterisk_client.requests.get", fake_get)

    assert ari_client().open_stored_recording("call-1", "items=0-10").status_code == 200
    assert captured["headers"] == {}


def test_best_effort_helpers_swallow_asterisk_errors(monkeypatch):
    class Failing:
        ok = False
        status_code = 500
        content = b""

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *args, **kwargs: Failing())

    client = ari_client()
    # Cleanup failures must never abort the call-teardown path.
    client.stop_recording("call-1")
    client.destroy_bridge("bridge-1")
    client.hangup("channel-1")


def test_hangup_ignores_a_missing_channel_id(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("hangup must not call ARI without a channel id")

    monkeypatch.setattr("app.asterisk_client.requests.request", fail)

    client = ari_client()
    client.hangup("")
    client.hangup(None)


def test_hangup_call_only_hangs_up_channels_that_exist(monkeypatch):
    hangups = []
    client = ari_client()
    monkeypatch.setattr(client, "hangup", hangups.append)

    client.hangup_call("employee-1", None)
    client.hangup_call(None, "customer-1")
    client.hangup_call("employee-1", "customer-1")

    assert hangups == ["employee-1", "customer-1", "employee-1", "customer-1"]
