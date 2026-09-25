from pathlib import Path

from app.models import CallStore
from app.services import TelephonyService


class DummyAsterisk:
    def __init__(self):
        self.created_call = None
        self.recording_stopped = []

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
