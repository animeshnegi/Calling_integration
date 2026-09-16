from datetime import datetime, timedelta, timezone

from app.models import Call, CallStore
from app.services import TelephonyService


class FakeAsterisk:
    def __init__(self):
        self.deleted = []

    def delete_stored_recording(self, name):
        self.deleted.append(name)
        return True


def test_recording_retention_uses_call_end_time(tmp_path):
    db = tmp_path / "calls.db"
    store = CallStore(str(db))
    old = datetime.now(timezone.utc) - timedelta(days=100)
    call = Call(
        call_id="old-call",
        contact_id=None,
        member_id=None,
        extension="101",
        phone="+13025551234",
        status="completed",
        answered=True,
        ended_at=old.isoformat(),
        recording_name="call-old-call",
        recording_format="wav",
        recording_status="finalized",
    )
    store.create(call)

    class Config:
        ARI_READY_PATH = str(tmp_path / "ari.ready")
        CALLS_DB_PATH = str(db)
        CRM_WEBHOOK_URL = ""
        CRM_WEBHOOK_TOKEN = ""
        DEFAULT_EXTENSION = "101"

    fake = FakeAsterisk()
    service = TelephonyService(fake, Config, store=store)
    service.cleanup_recordings()

    assert fake.deleted == ["call-old-call"]
    assert store.get("old-call").recording_status == "deleted"
    assert store.get("old-call").recording_path is None
