from __future__ import annotations

from pathlib import Path

from app.asterisk_client import AsteriskClient
from app.models import CallStore
from app.services import TelephonyService


class FakeConfig:
    ASTERISK_ARI_URL = "http://asterisk:8088/ari"
    ASTERISK_ARI_USER = "ari"
    ASTERISK_ARI_PASSWORD = "x" * 24
    ASTERISK_ARI_APP = "engineerip"
    ASTERISK_AMI_HOST = "asterisk"
    ASTERISK_AMI_PORT = 5038
    ASTERISK_AMI_USER = "ami"
    ASTERISK_AMI_PASSWORD = "x" * 24
    ASTERISK_DYNAMIC_CONFIG_PATH = "/tmp/pjsip.dynamic.conf"
    ARI_READY_PATH = "/tmp/engineerip-test-ari.ready"
    CALLS_DB_PATH = "/tmp/engineerip-test-calls.db"
    CRM_WEBHOOK_URL = ""
    CRM_WEBHOOK_TOKEN = ""
    DEFAULT_EXTENSION = "101"
    ASTERISK_EXTENSIONS = ("101",)


class FakeAsterisk:
    def __init__(self, service=None):
        self.service = service
        self.created_call_id = None
        self.customer_calls = []
        self.bridges = []
        self.recordings = []
        self.hangups = []
        self.destroyed_bridges = []

    def create_outbound_call(self, call_id, extension, phone, provider_endpoint, metadata=None):
        assert self.service.store.get(call_id) is not None
        self.created_call_id = call_id
        return call_id

    def create_customer_leg(self, call_id, phone, provider_endpoint, employee_channel_id):
        self.customer_calls.append((call_id, phone, provider_endpoint, employee_channel_id))
        return f"{call_id}-customer"

    def create_bridge(self, call_id):
        bridge_id = f"bridge-{call_id}"
        self.bridges.append(bridge_id)
        return bridge_id

    def add_channel_to_bridge(self, bridge_id, channel_id):
        pass

    def start_bridge_recording(self, bridge_id, name, fmt, beep, max_duration):
        self.recordings.append(name)
        return {"name": name}

    def play_bridge_media(self, bridge_id, media):
        return {"id": "playback-1", "media_uri": media}

    def stop_recording(self, name):
        pass

    def get_stored_recording(self, name):
        return {"name": name, "filename": f"/var/spool/asterisk/recording/{name}.wav", "format": "wav"}

    def cleanup_old_recordings(self, days):
        return 0

    def destroy_bridge(self, bridge_id):
        self.destroyed_bridges.append(bridge_id)

    def hangup(self, channel_id):
        self.hangups.append(channel_id)

    def hangup_call(self, employee_channel_id, customer_channel_id):
        for channel_id in (employee_channel_id, customer_channel_id):
            if channel_id:
                self.hangup(channel_id)

    def list_channels(self):
        return []

    def health(self):
        return {"status": "ok"}


def test_ari_variables_are_sent_in_json_body(monkeypatch):
    captured = {}

    class Response:
        ok = True
        content = b'{}'

        def json(self):
            return {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.asterisk_client.requests.request", fake_request)
    client = AsteriskClient(FakeConfig)
    client.create_outbound_call("call-1", "101", "+13025551234", "provider-1", {"contact_id": 42})

    assert captured["json"] == {"variables": {"EIP_CALL_ID": "call-1", "EIP_CONTACT_ID": "42"}}
    assert "variables" not in captured["params"]


def test_call_is_persisted_before_originate(tmp_path: Path):
    ready = tmp_path / "ari.ready"
    ready.touch()
    db = tmp_path / "calls.db"

    class Config(FakeConfig):
        ARI_READY_PATH = str(ready)
        CALLS_DB_PATH = str(db)

    store = CallStore(str(db))
    fake = FakeAsterisk()
    service = TelephonyService(fake, Config, store=store)
    fake.service = service

    call = service.start_outbound(phone="+13025551234", extension="101")

    assert call.call_id == fake.created_call_id
    assert store.get(call.call_id) is not None
    assert store.get(call.call_id).status == "ringing"


def test_full_employee_customer_bridge_recording_lifecycle(tmp_path: Path):
    ready = tmp_path / "ari.ready"
    ready.touch()
    db = tmp_path / "calls.db"

    class Config(FakeConfig):
        ARI_READY_PATH = str(ready)
        CALLS_DB_PATH = str(db)

    store = CallStore(str(db))
    fake = FakeAsterisk()
    service = TelephonyService(fake, Config, store=store)
    fake.service = service

    call = service.start_outbound(phone="+13025551234", extension="101")
    service.handle_ari_event({
        "type": "ChannelStateChange",
        "channel": {"id": call.employee_channel_id, "state": "Up"},
    })

    updated = store.get(call.call_id)
    assert updated.customer_channel_id == f"{call.call_id}-customer"
    assert updated.status == "dialing_customer"

    service.handle_ari_event({
        "type": "ChannelStateChange",
        "channel": {"id": updated.customer_channel_id, "state": "Up"},
    })
    updated = store.get(call.call_id)
    assert updated.answered is True
    assert updated.bridge_id == f"bridge-{call.call_id}"
    assert updated.recording_status == "recording"

    service.handle_ari_event({
        "type": "RecordingFinished",
        "recording": {"name": updated.recording_name},
    })
    updated = store.get(call.call_id)
    assert updated.recording_status == "finalized"
    assert updated.recording_path.endswith(".wav")

    service.handle_ari_event({
        "type": "ChannelDestroyed",
        "channel": {"id": updated.customer_channel_id},
    })
    updated = store.get(call.call_id)
    assert updated.status == "completed"
    assert updated.ended_at is not None
    assert updated.duration_seconds >= 0
    assert fake.destroyed_bridges == [f"bridge-{call.call_id}"]
