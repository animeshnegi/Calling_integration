from pathlib import Path

from app.models import Call, CallStore


def test_call_store_persists_and_finds_by_channel(tmp_path: Path):
    path = tmp_path / "calls.db"
    first = CallStore(str(path))
    call = Call(call_id="call-1", contact_id="c1", member_id="m1", extension="101", phone="+13025551234", employee_channel_id="call-1-employee")
    first.create(call)

    second = CallStore(str(path))
    loaded = second.get("call-1")
    assert loaded is not None
    assert loaded.contact_id == "c1"
    assert loaded.employee_channel_id == "call-1-employee"
    assert second.find_by_channel("call-1-employee").call_id == "call-1"

    second.update("call-1", status="answered", answered=True)
    assert first.get("call-1").answered is True
    assert first.get("call-1").status == "answered"
