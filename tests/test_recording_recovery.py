"""Bridge recordings must not be reported as "no playable audio" by mistake.

The console shows that message for any row whose recording_status is `failed`.
These regressions cover every way a healthy (or recoverable) recording used to
end up in that state, plus the teardown order that actually cut the audio short.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.admin import SettingsStore
from app.asterisk_client import AsteriskClient, AsteriskError
from app.models import Call
from test_services import _bridged_setup, make_service

RECORDING_SETTINGS = {
    "enabled": True, "format": "wav", "beep": False, "max_duration": 0,
    "announcement": False, "announcement_media": "",
}


def _call(call_id: str, **changes) -> Call:
    return Call(
        call_id=call_id, contact_id=None, member_id=None, extension="101",
        phone="+13025551234", **changes,
    )


def _answered_event():
    return {
        "type": "ChannelStateChange",
        "channel": {"id": "leg-answer", "state": "Up"},
        "timestamp": "2026-11-03T10:00:00Z",
    }


# ------------------------------------------------------------- teardown order

def test_finalize_stops_the_recorder_before_hanging_up_any_channel(tmp_path: Path):
    service, asterisk, store, call = _bridged_setup(tmp_path)
    name = f"call-{call.call_id}"
    store.update(
        call.call_id, status="answered", answered=True,
        bridge_id=f"bridge-{call.call_id}", recording_name=name,
        recording_status="recording",
    )
    order: list[tuple[str, str]] = []
    asterisk.stop_recording = lambda n: order.append(("stop", n))
    real_hangup = asterisk.hangup

    def tracked_hangup(*args, **kwargs):
        order.append(("hangup", args[0] if args else ""))
        return real_hangup(*args, **kwargs)

    asterisk.hangup = tracked_hangup

    service._finalize(call.call_id, "channel_destroyed")

    # Hanging up the callers first leaves Asterisk's internal Recorder channel
    # with no audio source. The file comes out empty/truncated and the stop
    # request arrives after the recorder is already gone.
    assert order[0] == ("stop", name), order
    assert any(kind == "hangup" for kind, _ in order[1:]), order
    assert all(kind == "hangup" for kind, _ in order[1:]), order


def test_a_failed_recording_is_still_stopped_at_teardown(tmp_path: Path):
    service, asterisk, store, call = _bridged_setup(tmp_path)
    name = f"call-{call.call_id}"
    store.update(
        call.call_id, status="answered", answered=True,
        bridge_id=f"bridge-{call.call_id}", recording_name=name,
        recording_status="failed",
    )

    service._finalize(call.call_id, "channel_destroyed")

    # A failed start/failure event may still have left a recorder attached to
    # the bridge; hanging up without stopping it leaks it.
    assert name in asterisk.recording_stopped


# ------------------------------------------------------- false "failed" rows

def test_crm_hiccup_after_a_successful_start_does_not_fail_the_recording(tmp_path: Path):
    service, asterisk, store, call = _bridged_setup(tmp_path)
    service._recording_settings = lambda extension: dict(RECORDING_SETTINGS)
    started = []
    asterisk.start_bridge_recording = lambda *args: started.append(args)

    def flaky_crm(event, call_obj, extra=None):
        if event == "call.recording_started":
            raise RuntimeError("CRM database is down")

    service.notify_crm = flaky_crm

    service.handle_ari_event(_answered_event())

    assert started, "the recorder should be running"
    assert store.get(call.call_id).recording_status == "recording"
    assert "leg-answer" not in asterisk.hung_up


def test_bridge_setup_failure_does_not_invent_a_failed_recording(tmp_path: Path):
    service, asterisk, store, call = _bridged_setup(tmp_path)

    def broken_bridge(*args):
        raise RuntimeError("bridge is gone")

    asterisk.add_channel_to_bridge = broken_bridge

    service.handle_ari_event(_answered_event())

    updated = store.get(call.call_id)
    assert updated.status == "failed"
    assert updated.recording_name is None
    assert updated.recording_status is None


def test_start_failure_with_stored_audio_is_recovered_not_failed(tmp_path: Path):
    """A 409 after a worker restart, or a lost response, is not lost audio."""
    service, asterisk, store, call = _bridged_setup(tmp_path)
    service._recording_settings = lambda extension: dict(RECORDING_SETTINGS)

    def conflict(*args):
        raise AsteriskError("Asterisk API 409: Recording already exists")

    asterisk.start_bridge_recording = conflict
    name = f"call-{call.call_id}"
    asterisk.get_stored_recording = lambda n: {
        "name": n, "format": "wav", "filename": f"/rec/{n}.wav",
    }

    service.handle_ari_event(_answered_event())

    updated = store.get(call.call_id)
    assert updated.recording_status == "finalized"
    assert updated.recording_path == f"/rec/{name}.wav"
    assert updated.bridge_id == f"bridge-{call.call_id}"   # call stays live
    assert "leg-answer" not in asterisk.hung_up
    assert "cust-1" not in asterisk.hung_up


# ------------------------------------------------------------ ARI event intake

def test_recording_failed_event_recovers_audio_asterisk_did_write(tmp_path: Path):
    service, asterisk, store = make_service(tmp_path)
    name = "call-f59ac5bf-22b4-492d-aba4-87ba05f5cd48"
    store.create(_call("f59ac5bf", recording_name=name, recording_status="recording"))
    asterisk.get_stored_recording = lambda n: {
        "name": n, "format": "wav", "filename": f"/rec/{n}.wav",
    }

    service.handle_ari_event({
        "type": "RecordingFailed",
        "recording": {"name": name, "cause": "Failed to answer channel"},
    })

    updated = store.get("f59ac5bf")
    assert updated.recording_status == "finalized"
    assert updated.recording_path == f"/rec/{name}.wav"


def test_recording_failed_event_logs_the_asterisk_cause_and_call_id(tmp_path: Path, caplog):
    service, asterisk, store = make_service(tmp_path)
    name = "call-f59ac5bf"
    store.create(_call("f59ac5bf", recording_name=name, recording_status="recording"))
    asterisk.get_stored_recording = lambda n: None

    with caplog.at_level(logging.ERROR, logger="app.services"):
        service.handle_ari_event({
            "type": "RecordingFailed",
            "recording": {"name": name, "cause": "Cannot record channel while in bridge"},
        })

    assert store.get("f59ac5bf").recording_status == "failed"
    assert "Cannot record channel while in bridge" in caplog.text
    assert "f59ac5bf" in caplog.text


def test_a_late_failure_never_downgrades_playable_audio(tmp_path: Path):
    service, asterisk, store = make_service(tmp_path)
    name = "call-finished"
    store.create(_call("finished", recording_name=name, recording_status="finalized"))

    service.handle_ari_event({
        "type": "RecordingFailed", "recording": {"name": name, "cause": "late"},
    })

    assert store.get("finished").recording_status == "finalized"


def test_recording_finished_without_a_stored_file_does_not_offer_dead_audio(tmp_path: Path):
    service, asterisk, store = make_service(tmp_path)
    name = "call-empty"
    store.create(_call("empty", recording_name=name, recording_status="recording"))
    asterisk.get_stored_recording = lambda n: None

    service.handle_ari_event({"type": "RecordingFinished", "recording": {"name": name}})

    # Reconciliation keeps looking while ARI is unavailable; the console must
    # not offer a player that can only answer 404.
    assert store.get("empty").recording_status == "finalizing"


# ------------------------------------------------------------- reconciliation

def test_reconcile_recovers_a_recent_failed_call_when_the_wav_exists(tmp_path: Path):
    service, asterisk, store = make_service(tmp_path)
    ended = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.create(_call("recent", recording_name="call-recent",
                       recording_status="failed", ended_at=ended))
    asterisk.get_stored_recording = lambda n: {
        "name": n, "format": "wav", "filename": f"/rec/{n}.wav",
    }

    service.reconcile_recordings()

    updated = store.get("recent")
    assert updated.recording_status == "finalized"
    assert updated.recording_path == "/rec/call-recent.wav"


def test_reconcile_leaves_a_failed_call_without_audio_failed(tmp_path: Path):
    service, asterisk, store = make_service(tmp_path)
    ended = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.create(_call("empty", recording_name="call-empty",
                       recording_status="failed", ended_at=ended))
    asterisk.get_stored_recording = lambda n: None

    service.reconcile_recordings()

    assert store.get("empty").recording_status == "failed"


def test_old_failed_attempts_are_not_polled_forever(tmp_path: Path):
    service, asterisk, store = make_service(tmp_path)
    ended = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    store.create(_call("old", recording_name="call-old",
                       recording_status="failed", ended_at=ended))
    queried = []
    asterisk.get_stored_recording = lambda n: queried.append(n) or {
        "name": n, "format": "wav", "filename": "/rec/x.wav",
    }

    service.reconcile_recordings()

    assert queried == []
    assert store.get("old").recording_status == "failed"


# ---------------------------------------------------------- error diagnostics

def test_ari_error_carries_the_reason_asterisk_reported(monkeypatch):
    class Refused:
        ok = False
        status_code = 422
        text = '{"message": "specified format is unknown on this system"}'
        content = b""

    monkeypatch.setattr("app.asterisk_client.requests.request", lambda *a, **k: Refused())
    client = AsteriskClient(type("Config", (), {
        "ASTERISK_ARI_URL": "http://asterisk:8088/ari",
        "ASTERISK_ARI_USER": "u", "ASTERISK_ARI_PASSWORD": "p",
        "ASTERISK_ARI_APP": "engineerip",
    }))

    with pytest.raises(AsteriskError) as exc:
        client.start_bridge_recording("bridge-1", "call-1", "slin16", False, 0)

    assert "422" in str(exc.value)
    assert "unknown on this system" in str(exc.value)


def test_legacy_slin16_format_is_normalized_to_a_real_asterisk_extension(tmp_path: Path):
    service, _, _ = make_service(tmp_path)

    class Store:
        def get_settings(self):
            return {"recording_format": "slin16"}

        def extension_row(self, extension):
            return {"active": True, "recording_enabled": 1}

        def recording_platform_enabled(self):
            return True

        def list_extensions(self):
            return []

    service.settings_store = Store()
    assert service._recording_settings("101")["format"] == "sln16"

    store = SettingsStore(str(tmp_path / "settings.db"), "test-secret")
    store.set_settings({"recording_format": "slin16"})
    assert store.get_settings()["recording_format"] == "sln16"
