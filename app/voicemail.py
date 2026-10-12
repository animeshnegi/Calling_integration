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


# A mailbox is the extension's three digits, or those digits and the number the
# extension belongs to (`101-13025550001`). The number is what keeps two lines'
# 101s - and their messages - apart.
MAILBOX_RE = re.compile(r"^[1-9]\d{2}(?:-\+?[0-9]{5,20})?$")
MAILBOX_GLOB_RE = re.compile(r"^[1-9]\d{2}(?:-[^/]*)?$")
MESSAGE_RE = re.compile(r"^msg\d{4}$")


def mailbox_name(mailbox: Any) -> str:
    """The folder a mailbox lives in: `<digits>-<number digits>`.

    `101@+13025550001` is the extension key and `101-13025550001` is its mailbox -
    the name the renderer writes, the credentials sheet shows and the console
    uses. A three-digit extension is resolved only within the current phone
    number, so two lines' 101s are two mailboxes and the digits alone name a
    mailbox only where there is one - the platform's own rows, and the folders of
    a deployment that predates per-number extension sets.
    """
    text = str(mailbox or "").strip()
    digits, _, scope = text.partition("@")
    if scope:
        scope_digits = re.sub(r"[^0-9]", "", scope)
        return f"{digits}-{scope_digits}" if digits.isdigit() and scope_digits else text
    head, sep, tail = text.partition("-")
    if sep and head.isdigit():
        # One spelling per mailbox: `101-+13025550001` and `101-13025550001` name
        # the same folder, and it is the second the renderer writes.
        return f"{head}-{re.sub(r'[^0-9]', '', tail)}"
    return text
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
        mailbox = mailbox_name(mailbox)
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
        mailbox = mailbox_name(mailbox) if mailbox else mailbox
        if mailbox and not MAILBOX_RE.fullmatch(str(mailbox)):
            raise ValueError("Invalid voicemail mailbox")
        if folder and folder.lower() not in FOLDERS:
            raise ValueError("Invalid voicemail folder")
        context_root = self.root.joinpath(self.context)
        mailboxes = (
            [str(mailbox)] if mailbox
            else sorted(p.name for p in context_root.iterdir() if p.is_dir() and MAILBOX_GLOB_RE.fullmatch(p.name))
            if context_root.is_dir() else []
        )
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

    def adopt_mailbox(self, legacy: str, mailbox: str) -> int:
        """Move a pre-scoped mailbox's messages into its number-scoped mailbox.

        Before extensions belonged to a phone number, the mailbox of `101` was
        `101` and its messages sat in that folder. The same device now files under
        `101-13025550001`, so an upgrade moves what is already there - renumbered
        so nothing is overwritten - rather than leaving the customer's messages
        behind in a folder nothing reads any more. A no-op the second time it
        runs, and when `legacy` already is the mailbox.
        """
        legacy, mailbox = mailbox_name(legacy), mailbox_name(mailbox)
        if legacy == mailbox or not MAILBOX_RE.fullmatch(str(legacy)) or not MAILBOX_RE.fullmatch(str(mailbox)):
            return 0
        source_root = self.root / self.context / str(legacy)
        if not source_root.is_dir():
            return 0
        moved = 0
        with self._locked():
            for folder_key, folder_dir in FOLDERS.items():
                source = source_root / folder_dir
                if not source.is_dir():
                    continue
                stems = sorted(
                    path.stem for path in source.glob("msg[0-9][0-9][0-9][0-9].txt")
                    if path.is_file() and not path.is_symlink() and MESSAGE_RE.fullmatch(path.stem)
                )
                if not stems:
                    continue
                destination = self._folder(str(mailbox), folder_key)
                existing = {path.stem for path in destination.glob("msg[0-9][0-9][0-9][0-9].*")}
                for stem in stems:
                    index = 0
                    while f"msg{index:04d}" in existing:
                        index += 1
                    target = f"msg{index:04d}"
                    for path in sorted(source.glob(f"{stem}.*")):
                        if path.is_file() and not path.is_symlink():
                            shutil.move(str(path), str(destination / f"{target}{path.suffix}"))
                    existing.add(target)
                    moved += 1
            # The folders of one mailbox, now empty, and nothing outside them.
            for folder_dir in FOLDERS.values():
                empty = source_root / folder_dir
                if empty.is_dir() and not any(empty.iterdir()):
                    empty.rmdir()
            if not any(source_root.iterdir()):
                source_root.rmdir()
        return moved

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
