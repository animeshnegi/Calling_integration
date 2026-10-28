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

from . import local_transcription
from .voicemail import mailbox_name

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
ELASTIC_EMAIL_URL = "https://api.elasticemail.com/v2/email/send"
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
        """Build one provider-neutral message, keeping audio as bytes until send."""
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
            "to": recipient, "from_email": config["from_email"],
            "from_name": config.get("from_name") or "EngineerIP Voicemail",
            "subject": subject, "text": text, "html": markup,
        }
        if audio:
            body["attachment"] = {
                "content": audio_bytes if audio_bytes is not None else audio.read_bytes(),
                "type": "audio/wav" if audio.suffix.lower() == ".wav" else "audio/gsm",
                "filename": f"voicemail-{mailbox}-{message['message']}{audio.suffix.lower()}",
            }
        return body

    @staticmethod
    def _sendgrid(config: dict[str, Any], message: dict[str, Any]) -> tuple[bool, str | None]:
        payload: dict[str, Any] = {
            "personalizations": [{"to": [{"email": message["to"]}]}],
            "from": {"email": message["from_email"], "name": message["from_name"]},
            "subject": message["subject"],
            "content": [{"type": "text/plain", "value": message["text"]},
                        {"type": "text/html", "value": message["html"]}],
        }
        attachment = message.get("attachment")
        if attachment:
            payload["attachments"] = [{
                "content": base64.b64encode(attachment["content"]).decode("ascii"),
                "type": attachment["type"], "filename": attachment["filename"], "disposition": "attachment",
            }]
        response = requests.post(
            SENDGRID_URL, json=payload,
            headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
            timeout=(5, 15), allow_redirects=False,
        )
        if response.status_code == 202:
            return True, None
        # Never echo provider bodies; they may contain credentials or private data.
        hints = {
            400: "Check the sender and recipient email addresses and sender name.",
            401: "The API key is invalid or revoked. Save a replacement SendGrid key.",
            403: "Verify the sender in SendGrid, Mail Send permission, account status and IP restrictions.",
            413: "The attachment is too large.",
            429: "SendGrid rate limit reached. Retry later.",
        }
        hint = hints.get(response.status_code, "SendGrid is unavailable; retry later." if response.status_code >= 500 else "Unexpected SendGrid response; check the account configuration.")
        return False, f"SendGrid HTTP {response.status_code}: {hint}"

    @staticmethod
    def _elastic_error(status: int, detail: Any) -> str:
        # Use untrusted provider text only to select fixed hints, never display it.
        text = detail[:4000].lower() if isinstance(detail, str) else ""
        if status == 429 or any(word in text for word in ("rate limit", "too many requests", "throttl")):
            hint = "Rate limit reached. Retry later."
        elif status == 413 or any(word in text for word in ("too large", "size limit", "maximum size")):
            hint = "The message or audio attachment is too large for the account's email size limit."
        elif status >= 500:
            hint = "Elastic Email is unavailable; retry later."
        elif any(word in text for word in ("api key", "apikey", "access token", "unauthorized", "not authorized", "permission", "access level")) or status == 401:
            hint = "Check the Elastic Email API key, Send HTTP permission, expiry and IP restrictions."
        elif any(word in text for word in ("sender", "from address", "from email", "domain", "verif")):
            hint = "Verify the sender address/domain in Elastic Email and check its sending permissions."
        elif any(word in text for word in ("credit", "balance", "account", "suspend")):
            hint = "Check the Elastic Email account status, plan and sending credits."
        else:
            hint = "Check the API key's Send HTTP permission, verified sender/domain and account status."
        if status == 200:
            return f"Elastic Email rejected the message (HTTP 200): {hint}"
        return f"Elastic Email HTTP {status}: {hint}"

    @staticmethod
    def _elastic(config: dict[str, Any], message: dict[str, Any]) -> tuple[bool, str | None]:
        # Match Elastic Email's v2 form API. The key belongs in the POST body,
        # never the URL or a SendGrid Authorization header. With audio, requests
        # encodes the same fields and file as multipart/form-data.
        data = {
            "apikey": config["api_key"], "subject": message["subject"],
            "from": message["from_email"], "fromName": message["from_name"],
            "to": message["to"], "bodyText": message["text"], "bodyHtml": message["html"],
            "isTransactional": "true",
        }
        options: dict[str, Any] = {"data": data, "timeout": (5, 30), "allow_redirects": False}
        attachment = message.get("attachment")
        if attachment:
            options["files"] = [("file", (attachment["filename"], attachment["content"], attachment["type"]))]
        response = requests.post(ELASTIC_EMAIL_URL, **options)
        try:
            result = response.json()
        except ValueError:
            result = None
        # A 200 with {success:false} is a failure, not acceptance. When the
        # provider's response is malformed, acceptance is unknown: don't mark it
        # delivered and tell the administrator to check activity before retrying.
        if response.status_code == 200 and isinstance(result, dict) and result.get("success") is True:
            return True, None
        if response.status_code == 200 and (not isinstance(result, dict) or result.get("success") is not False):
            return False, "Elastic Email returned an unexpected response; acceptance is unknown. Check account activity before retrying."
        detail = result.get("error") if isinstance(result, dict) else None
        return False, VoicemailEmailNotifier._elastic_error(response.status_code, detail)

    @staticmethod
    def _send(config: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str | None]:
        provider = config.get("provider", "sendgrid")
        labels = {"sendgrid": "SendGrid", "elasticemail": "Elastic Email"}
        if provider not in labels:
            return False, "Choose a valid email provider before sending."
        name = labels[provider]
        try:
            if provider == "elasticemail":
                return VoicemailEmailNotifier._elastic(config, payload)
            return VoicemailEmailNotifier._sendgrid(config, payload)
        except requests.Timeout:
            return False, f"{name} request timed out. Check server connectivity and account activity before retrying; acceptance is unknown."
        except requests.RequestException:
            return False, f"Cannot reach {name}. Check server DNS, HTTPS connectivity and TLS certificates."

    @staticmethod
    def _transcribe(config, audio: Path, audio_bytes: bytes) -> tuple[str | None, str]:
        if not config.get("transcription_enabled"):
            return None, "disabled"
        return local_transcription.transcribe(audio_bytes, audio.suffix)

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
        provider = {"sendgrid": "SendGrid", "elasticemail": "Elastic Email"}.get(config.get("provider", "sendgrid"), "email provider")
        if not config.get("api_key"):
            return False, config.get("api_key_error") or f"Save a {provider} API key before sending a test."
        if not config.get("from_email"):
            return False, f"Save a verified {provider} sender email before sending a test."
        message = {"mailbox": "test", "caller_id": "EngineerIP test", "received_at": "now", "duration_seconds": 0, "message": "test"}
        payload = self._payload(config, recipient, message, None)
        payload["subject"] = "EngineerIP email delivery test"
        payload["text"] = "Your EngineerIP email configuration is working. This test contains no voicemail audio and does not test transcription."
        payload["html"] = "<p>Your EngineerIP email configuration is working.</p><p>This test contains no voicemail audio and does not test transcription.</p>"
        return self._send(config, payload)
