from __future__ import annotations

import base64
import hashlib
import html
import logging
import threading
import time
from pathlib import Path
from typing import Any

import requests

from .voicemail import mailbox_name

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_MESSAGES_PER_SCAN = 20
logger = logging.getLogger(__name__)


class VoicemailEmailNotifier:
    def __init__(self, voicemail_store, settings_store):
        self.voicemail_store = voicemail_store
        self.settings_store = settings_store
        self._thread_lock = threading.Lock()
        self._thread = None

    def process_background(self) -> bool:
        """Never hold up ARI maintenance/IVR timers waiting for email/cloud IO."""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=self._run_background, name="voicemail-email", daemon=True)
            self._thread.start()
            return True

    def _run_background(self):
        try:
            self.process()
        except Exception:
            logger.exception("Voicemail email scan failed")

    @staticmethod
    def _fingerprint(message: dict[str, Any], audio: bytes) -> str:
        # Stable across msgNNNN renumbering/folder moves, but two different
        # calls containing identical audio must not suppress one another.
        identity = "\0".join((message["mailbox"], message.get("received_at", ""), message.get("caller_channel", "")))
        return hashlib.sha256(b"voicemail-v2\0" + identity.encode() + b"\0" + audio).hexdigest()

    @staticmethod
    def _payload(config, recipient, message, audio: Path | None, *, audio_bytes=None,
                 transcript=None, transcription_status="disabled") -> dict[str, Any]:
        mailbox = message["mailbox"]
        caller = str(message.get("caller_id") or "Unknown caller")[:300]
        received = str(message.get("received_at") or "")
        duration = int(message.get("duration_seconds") or 0)
        subject = " ".join(f"New voicemail for extension {mailbox} from {caller}".split())[:998]
        text = f"A new voicemail was received for extension {mailbox}.\n\nCaller: {caller}\nReceived: {received}\nDuration: {duration} seconds\n"
        markup = f"<p>A new voicemail was received for extension <strong>{html.escape(mailbox)}</strong>.</p><p><strong>Caller:</strong> {html.escape(caller)}<br><strong>Received:</strong> {html.escape(received)}<br><strong>Duration:</strong> {duration} seconds</p>"
        if transcript:
            text += f"\nVoicemail transcript (machine-generated; may contain errors):\n\n{transcript}\n\nThe original audio is attached.\n"
            markup += '<h3>Voicemail transcript</h3><p>Machine-generated; may contain errors. The original audio is attached.</p><p>' + html.escape(transcript).replace("\n", "<br>") + '</p>'
        elif audio and transcription_status != "disabled":
            fallback = "Transcription is unavailable for this message. Please listen to the attached audio."
            text += f"\n{fallback}\n"
            markup += f"<p>{fallback}</p>"
        body: dict[str, Any] = {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {"email": config["from_email"], "name": config.get("from_name") or "EngineerIP Voicemail"},
            "subject": subject,
            "content": [{"type": "text/plain", "value": text}, {"type": "text/html", "value": markup}],
        }
        if audio:
            body["attachments"] = [{
                "content": base64.b64encode(audio_bytes if audio_bytes is not None else audio.read_bytes()).decode("ascii"),
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
                timeout=(5, 15), allow_redirects=False,
            )
            if response.status_code == 202:
                return True, None
            # Do not echo provider response bodies: they may reflect credentials,
            # recipient addresses or other untrusted/sensitive request content.
            hints = {
                400: "Check the sender and recipient email addresses and sender name.",
                401: "The API key is invalid or revoked. Save a replacement SendGrid key.",
                403: "Verify the sender in SendGrid, Mail Send permission, account status and IP restrictions.",
                413: "The attachment is too large.",
                429: "SendGrid rate limit reached. Retry later.",
            }
            hint = hints.get(response.status_code, "SendGrid is unavailable; retry later." if response.status_code >= 500 else "Unexpected SendGrid response; check the account configuration.")
            return False, f"SendGrid HTTP {response.status_code}: {hint}"
        except requests.Timeout:
            return False, "SendGrid request timed out. Check server connectivity and retry; acceptance is unknown."
        except requests.RequestException:
            return False, "Cannot reach SendGrid. Check server DNS, HTTPS connectivity and TLS certificates."

    @staticmethod
    def _transcribe(config, audio: Path, audio_bytes: bytes) -> tuple[str | None, str]:
        if not config.get("transcription_enabled"):
            return None, "disabled"
        if audio.suffix.lower() != ".wav":
            return None, "unsupported_audio"
        if not config.get("transcription_api_key"):
            return None, "key_unavailable"
        try:
            response = requests.post(
                TRANSCRIPTION_URL,
                headers={"Authorization": f"Bearer {config['transcription_api_key']}"},
                # No mailbox/recipient/caller metadata is sent to the provider.
                files={"file": ("voicemail.wav", audio_bytes, "audio/wav")},
                data={"model": "whisper-1"}, timeout=(5, 45), allow_redirects=False,
            )
            if response.status_code != 200:
                return None, {401: "invalid_key", 403: "access_denied", 429: "quota_or_rate_limit"}.get(response.status_code, "provider_error")
            result = response.json()
            text = result.get("text") if isinstance(result, dict) else None
            if not isinstance(text, str) or not text.strip():
                return None, "empty_transcript"
            return text.strip()[:20000], "transcribed"
        except requests.Timeout:
            return None, "timeout"
        except (requests.RequestException, ValueError):
            return None, "provider_error"

    def _snapshot(self, candidate):
        # Mailbox operations rename msgNNNN files. Snapshot metadata AND audio
        # under the same lock so a delete/read cannot attach a different message
        # to a stale caller/date. Release the lock before any network request.
        with self.voicemail_store._locked():
            current = next((row for row in self.voicemail_store.list_messages(candidate["mailbox"], candidate["folder"])
                            if row["message"] == candidate["message"]), None)
            if not current:
                return None
            audio = self.voicemail_store.audio_path(current["mailbox"], current["folder"], current["message"])
            if not audio:
                return None
            stat = audio.stat()
            if stat.st_size > MAX_ATTACHMENT_BYTES or time.time() - stat.st_mtime < 10:
                return None
            audio_bytes = audio.read_bytes()
            if len(audio_bytes) > MAX_ATTACHMENT_BYTES:
                return None
            return current, audio, audio_bytes

    def process(self) -> int:
        config = self.settings_store.get_email_config(include_key=True)
        if not config.get("enabled") or not config.get("api_key") or not config.get("from_email"):
            return 0
        # Public extension digits are NOT globally unique. Never use a digits
        # fallback: extension 101 on two numbers may belong to different tenants.
        extensions = {mailbox_name(row.get("key") or row["extension"]): row
                      for row in self.settings_store.list_extensions()
                      if row.get("active") and row.get("voicemail_enabled")}
        delivered = attempts = 0
        for candidate in self.voicemail_store.list_messages():
            if candidate["folder"] not in {"inbox", "urgent"}:
                continue
            extension = extensions.get(candidate["mailbox"])
            recipient = str((extension or {}).get("voicemail_email") or "").strip()
            if not recipient:
                continue
            try:
                snapshot = self._snapshot(candidate)
            except (OSError, ValueError):
                continue  # One removed/incomplete message must not stop the scan.
            if not snapshot:
                continue
            message, audio, audio_bytes = snapshot
            fingerprint = self._fingerprint(message, audio_bytes)
            if not self.settings_store.claim_voicemail_delivery(
                    fingerprint, message["mailbox"], recipient,
                    legacy_fingerprint=hashlib.sha256(message["mailbox"].encode() + audio_bytes).hexdigest(),
                    received_at=message.get("received_at")):
                continue
            transcript, transcription_status = self._transcribe(config, audio, audio_bytes)
            success, error = self._send(config, self._payload(config, recipient, message, audio,
                                                           audio_bytes=audio_bytes, transcript=transcript,
                                                           transcription_status=transcription_status))
            self.settings_store.finish_voicemail_delivery(fingerprint, success, error, transcription_status)
            delivered += int(success)
            attempts += 1
            if attempts >= MAX_MESSAGES_PER_SCAN:
                break
        return delivered

    def send_test(self, recipient: str) -> tuple[bool, str | None]:
        config = self.settings_store.get_email_config(include_key=True)
        # Test credentials without enabling automatic voicemail notifications.
        if not config.get("api_key"):
            return False, config.get("api_key_error") or "Save a SendGrid API key before sending a test."
        if not config.get("from_email"):
            return False, "Save a verified SendGrid sender email before sending a test."
        message = {"mailbox": "test", "caller_id": "EngineerIP test", "received_at": "now", "duration_seconds": 0, "message": "test"}
        payload = self._payload(config, recipient, message, None)
        payload["subject"] = "EngineerIP email delivery test"
        payload["content"] = [
            {"type": "text/plain", "value": "Your EngineerIP email configuration is working. This test contains no voicemail audio and does not test transcription."},
            {"type": "text/html", "value": "<p>Your EngineerIP email configuration is working.</p><p>This test contains no voicemail audio and does not test transcription.</p>"},
        ]
        return self._send(config, payload)
