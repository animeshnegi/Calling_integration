from pathlib import Path

from app.voicemail import VoicemailStore


def create_message(root: Path, mailbox="101", folder="INBOX", stem="msg0000"):
    directory = root / "engineerip" / mailbox / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.txt").write_text(
        "[message]\ncallerid=Customer <+13025550123>\norigtime=1700000000\nduration=42\n",
        encoding="utf-8",
    )
    (directory / f"{stem}.wav").write_bytes(b"RIFF-demo")


def test_list_play_mark_read_and_delete(tmp_path):
    create_message(tmp_path)
    store = VoicemailStore(str(tmp_path))

    messages = store.list_messages("101", "inbox")
    assert len(messages) == 1
    assert messages[0]["duration_seconds"] == 42
    assert messages[0]["caller_id"].startswith("Customer")
    assert store.audio_path("101", "inbox", "msg0000").read_bytes() == b"RIFF-demo"

    assert store.mark_read("101", "inbox", "msg0000") is True
    assert store.list_messages("101", "inbox") == []
    assert store.list_messages("101", "old")[0]["message"] == "msg0000"

    assert store.delete("101", "old", "msg0000") is True
    assert store.summary()["total"] == 0


def test_invalid_voicemail_paths_are_rejected(tmp_path):
    store = VoicemailStore(str(tmp_path))
    assert store.audio_path("../101", "inbox", "msg0000") is None
    assert store.delete("101", "../../etc", "msg0000") is False
