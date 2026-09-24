from __future__ import annotations

import base64
import hashlib
import html
import time
from pathlib import Path
from typing import Any

import requests


SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class VoicemailEmailNotifier:
    def __init__(self, voicemail_store, settings_store):
        self.voicemail_store = voicemail_store
        self.settings_store = settings_store

    @staticmethod
    def _fingerprint(message: dict[str, Any], audio: Path) -> str:
        digest = hashlib.sha256()
        digest.update(message["mailbox"].encode())
        with audio.open("rb") as handle:
            for chunk in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _payload(config: dict[str, Any], recipient: str, message: dict[str, Any], audio: Path | None) -> dict[str, Any]:
        mailbox = message["mailbox"]
        caller = str(message.get("caller_id") or "Unknown caller")[:300]
        received = str(message.get("received_at") or "")
        duration = int(message.get("duration_seconds") or 0)
        subject = f"New voicemail for extension {mailbox} from {caller}"[:998]
        text = f"A new voicemail was received for extension {mailbox}.\n\nCaller: {caller}\nReceived: {received}\nDuration: {duration} seconds\n"
        body: dict[str, Any] = {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {"email": config["from_email"], "name": config.get("from_name") or "EngineerIP Voicemail"},
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": text},
                {"type": "text/html", "value": f"<p>A new voicemail was received for extension <strong>{html.escape(mailbox)}</strong>.</p><p><strong>Caller:</strong> {html.escape(caller)}<br><strong>Received:</strong> {html.escape(received)}<br><strong>Duration:</strong> {duration} seconds</p>"},
            ],
        }
        if audio:
            body["attachments"] = [{
                "content": base64.b64encode(audio.read_bytes()).decode("ascii"),
                "type": "audio/wav" if audio.suffix.lower() == ".wav" else "audio/gsm",
                "filename": f"voicemail-{mailbox}-{message['message']}{audio.suffix.lower()}",
                "disposition": "attachment",
            }]
        return body

    @staticmethod
    def _send(config: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str | None]:
        try:
            response = requests.post(
                SENDGRID_URL, json=payload,
                headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
                timeout=15,
            )
            if response.status_code == 202:
                return True, None
            return False, f"SendGrid HTTP {response.status_code}"
        except requests.RequestException as exc:
            return False, exc.__class__.__name__

    def process(self) -> int:
        config = self.settings_store.get_email_config(include_key=True)
        if not config.get("enabled") or not config.get("api_key"):
            return 0
        extensions = {row["extension"]: row for row in self.settings_store.list_extensions()}
        delivered = 0
        for message in self.voicemail_store.list_messages():
            if message["folder"] not in {"inbox", "urgent"}:
                continue
            extension = extensions.get(message["mailbox"])
            recipient = str((extension or {}).get("voicemail_email") or "").strip()
            if not recipient:
                continue
            audio = self.voicemail_store.audio_path(message["mailbox"], message["folder"], message["message"])
            if not audio or audio.stat().st_size > MAX_ATTACHMENT_BYTES:
                continue
            # Avoid reading a file while Asterisk is still finalizing it.
            if time.time() - audio.stat().st_mtime < 10:
                continue
            fingerprint = self._fingerprint(message, audio)
            if not self.settings_store.claim_voicemail_delivery(fingerprint, message["mailbox"], recipient):
                continue
            success, error = self._send(config, self._payload(config, recipient, message, audio))
            self.settings_store.finish_voicemail_delivery(fingerprint, success, error)
            delivered += int(success)
        return delivered

    def send_test(self, recipient: str) -> tuple[bool, str | None]:
        config = self.settings_store.get_email_config(include_key=True)
        if not config.get("enabled") or not config.get("api_key"):
            return False, "SendGrid email is not enabled"
        message = {"mailbox": "test", "caller_id": "EngineerIP test", "received_at": "now", "duration_seconds": 0, "message": "test"}
        return self._send(config, self._payload(config, recipient, message, None))
