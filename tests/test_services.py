from pathlib import Path

from app.models import CallStore
from app.services import TelephonyService


class DummyAsterisk:
    def __init__(self):
        self.created_call = None
        self.recording_stopped = []

    def create_outbound_call(self, call_id, extension, phone, provider_endpoint, metadata):
        self.created_call = call_id
        assert self.service.store.get(call_id) is not None
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


def test_call_is_stored_before_asterisk_originate(tmp_path: Path):
    asterisk = DummyAsterisk()
    store = CallStore(str(tmp_path / "calls.db"))
    service = TelephonyService(asterisk, store=store)
    asterisk.service = service

    call = service.start_outbound(phone="+13025551234", extension="101")
    assert call.call_id == asterisk.created_call
    assert store.get(call.call_id).status == "ringing"


def test_recording_finished_event_is_correlated_without_channel(tmp_path: Path):
    asterisk = DummyAsterisk()
    store = CallStore(str(tmp_path / "calls.db"))
    service = TelephonyService(asterisk, store=store)
    call = service.start_outbound(phone="+13025551234", extension="101")
    store.update(call.call_id, recording_name=f"call-{call.call_id}", recording_status="recording")

    service.handle_ari_event({"type": "RecordingFinished", "recording": {"name": f"call-{call.call_id}"}})
    assert store.get(call.call_id).recording_status == "finalized"
