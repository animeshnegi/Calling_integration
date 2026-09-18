from __future__ import annotations

import configparser
import fcntl
import re
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAILBOX_RE = re.compile(r"^[1-9]\d{2}$")
MESSAGE_RE = re.compile(r"^msg\d{4}$")
FOLDERS = {"inbox": "INBOX", "old": "Old", "urgent": "Urgent"}
AUDIO_FORMATS = ("wav", "WAV", "gsm")


class VoicemailStore:
    """Safely manage Asterisk app_voicemail message files on a private volume."""

    def __init__(self, root: str, context: str = "engineerip") -> None:
        self.root = Path(root)
        self.context = context
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def _folder(self, mailbox: str, folder: str) -> Path:
        if not MAILBOX_RE.fullmatch(str(mailbox)) or folder.lower() not in FOLDERS:
            raise ValueError("Invalid voicemail mailbox or folder")
        context_root = (self.root / self.context).resolve()
        path = self.root / self.context / str(mailbox) / FOLDERS[folder.lower()]
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve()
        if context_root not in resolved.parents:
            raise ValueError("Invalid voicemail path")
        return resolved

    @staticmethod
    def _metadata(path: Path) -> dict[str, str]:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
            return dict(parser["message"]) if parser.has_section("message") else {}
        except (OSError, configparser.Error, UnicodeError):
            return {}

    @staticmethod
    def _audio_path(folder: Path, stem: str) -> Path | None:
        for extension in AUDIO_FORMATS:
            candidate = folder / f"{stem}.{extension}"
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        return None

    def list_messages(self, mailbox: str | None = None, folder: str | None = None) -> list[dict[str, Any]]:
        if mailbox and not MAILBOX_RE.fullmatch(str(mailbox)):
            raise ValueError("Invalid voicemail mailbox")
        if folder and folder.lower() not in FOLDERS:
            raise ValueError("Invalid voicemail folder")
        mailboxes = [str(mailbox)] if mailbox else [p.name for p in self.root.joinpath(self.context).glob("[1-9][0-9][0-9]") if p.is_dir()]
        folder_keys = [folder.lower()] if folder else list(FOLDERS)
        result: list[dict[str, Any]] = []
        for box in sorted(mailboxes):
            if not MAILBOX_RE.fullmatch(box):
                continue
            for folder_key in folder_keys:
                try:
                    directory = self._folder(box, folder_key)
                except ValueError:
                    continue
                for text_file in sorted(directory.glob("msg[0-9][0-9][0-9][0-9].txt"), reverse=True):
                    stem = text_file.stem
                    if text_file.is_symlink() or not MESSAGE_RE.fullmatch(stem):
                        continue
                    audio = self._audio_path(directory, stem)
                    if not audio:
                        continue
                    meta = self._metadata(text_file)
                    try:
                        timestamp = datetime.fromtimestamp(int(meta.get("origtime", "0")), timezone.utc).isoformat()
                    except (TypeError, ValueError, OSError):
                        timestamp = datetime.fromtimestamp(audio.stat().st_mtime, timezone.utc).isoformat()
                    try:
                        duration = max(0, int(meta.get("duration", "0") or 0))
                    except (TypeError, ValueError):
                        duration = 0
                    result.append({
                        "id": f"{box}:{folder_key}:{stem}", "mailbox": box, "folder": folder_key,
                        "message": stem, "caller_id": meta.get("callerid", "Unknown caller"),
                        "caller_channel": meta.get("callerchan", ""), "duration_seconds": duration,
                        "received_at": timestamp, "format": audio.suffix.lstrip(".").lower(), "size_bytes": audio.stat().st_size,
                    })
        return sorted(result, key=lambda item: item["received_at"], reverse=True)

    def audio_path(self, mailbox: str, folder: str, message: str) -> Path | None:
        if not MESSAGE_RE.fullmatch(message):
            return None
        try:
            return self._audio_path(self._folder(mailbox, folder), message)
        except ValueError:
            return None

    @contextmanager
    def _locked(self):
        with self._lock:
            lock_path = self.root / ".voicemail.lock"
            with lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _message_files(directory: Path, stem: str) -> list[Path]:
        return [path for path in directory.glob(f"{stem}.*") if path.is_file() and not path.is_symlink()]

    def _renumber(self, directory: Path) -> None:
        stems = sorted({path.stem for path in directory.glob("msg[0-9][0-9][0-9][0-9].*") if MESSAGE_RE.fullmatch(path.stem)})
        staged: list[tuple[Path, str]] = []
        for index, stem in enumerate(stems):
            for path in self._message_files(directory, stem):
                temporary = path.with_name(f".renumber-{index:04d}-{path.name}")
                path.rename(temporary)
                staged.append((temporary, f"msg{index:04d}{path.suffix}"))
        for temporary, final_name in staged:
            temporary.rename(directory / final_name)

    def delete(self, mailbox: str, folder: str, message: str) -> bool:
        if not MESSAGE_RE.fullmatch(message):
            return False
        with self._locked():
            try:
                directory = self._folder(mailbox, folder)
            except ValueError:
                return False
            files = self._message_files(directory, message)
            if not files:
                return False
            for path in files:
                path.unlink(missing_ok=True)
            self._renumber(directory)
            return True

    def mark_read(self, mailbox: str, folder: str, message: str) -> bool:
        if folder.lower() != "inbox" or not MESSAGE_RE.fullmatch(message):
            return False
        with self._locked():
            source = self._folder(mailbox, "inbox")
            files = self._message_files(source, message)
            if not files:
                return False
            destination = self._folder(mailbox, "old")
            existing = {path.stem for path in destination.glob("msg[0-9][0-9][0-9][0-9].*")}
            index = 0
            while f"msg{index:04d}" in existing:
                index += 1
            target_stem = f"msg{index:04d}"
            for path in files:
                shutil.move(str(path), str(destination / f"{target_stem}{path.suffix}"))
            self._renumber(source)
            self._renumber(destination)
            return True

    def summary(self) -> dict[str, Any]:
        messages = self.list_messages()
        return {
            "total": len(messages), "new": sum(item["folder"] == "inbox" for item in messages),
            "old": sum(item["folder"] == "old" for item in messages),
            "urgent": sum(item["folder"] == "urgent" for item in messages),
        }
