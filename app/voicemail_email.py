from __future__ import annotations

import hashlib
import html
import logging
import threading
import time
from pathlib import Path
from typing import Any

import requests

from . import local_transcription
from .telephony_config import configured_voicemail
from .voicemail import mailbox_name

ELASTIC_EMAIL_URL = "https://api.elasticemail.com/v2/email/send"
AUTOMATED_NOTICE = (
    "This is an automated message. The sending email address is not associated "
    "with a user. Please do not reply."
)
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
        text += f"\n{AUTOMATED_NOTICE}\n"
        markup += f"<p>{html.escape(AUTOMATED_NOTICE)}</p>"
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
        # Legacy rows and stale browsers may still say provider=sendgrid. The
        # only permitted sender is Elastic Email, independent of that field.
        # Avoid forwarding a recognisable SendGrid key to another provider.
        if str(config.get("api_key") or "").upper().startswith("SG."):
            return False, "The saved key appears to be a SendGrid key. Save an Elastic Email API key instead."
        try:
            return VoicemailEmailNotifier._elastic(config, payload)
        except requests.Timeout:
            return False, "Elastic Email request timed out. Check server connectivity and account activity before retrying; acceptance is unknown."
        except requests.RequestException:
            return False, "Cannot reach Elastic Email. Check server DNS, HTTPS connectivity and TLS certificates."

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
            return current, audio, audio_bytes, stat.st_mtime_ns // 1_000_000

    def _mailbox_recipients(self) -> dict[str, tuple[str, int | None, int]]:
        """Resolve recipients and automatic-delivery cutoffs without guessing."""
        # Use the same mailbox decisions as the Asterisk renderer: a voicemail
        # step in either flow is sufficient, even with the separate desk switch
        # off. Never match the three digits across number-scoped mailboxes.
        all_rows = self.settings_store.list_extensions()
        rows = [row for row in all_rows if row.get("active")]
        live_keys, _ = configured_voicemail(self.settings_store, rows)
        users = {str(user["id"]): user for user in self.settings_store.list_users()
                 if user.get("active") and user.get("role") == "user"}
        # Legacy platform-owned mailboxes may have an explicitly assigned user
        # profile instead of an owner. Match a full key or globally unique digits
        # (including inactive extensions); multiple profiles are ambiguous too.
        by_key = {row["key"]: row for row in all_rows}
        by_digits: dict[str, list[dict]] = {}
        for row in all_rows:
            by_digits.setdefault(row["digits"], []).append(row)
        profile_users: dict[str, list[dict]] = {}
        for user in users.values():
            assigned = str(user.get("extension") or "").strip()
            if not assigned:
                continue
            scoped = by_key.get(assigned) if "@" in assigned else None
            matches = ([scoped] if scoped else []) if "@" in assigned else by_digits.get(assigned, [])
            if len(matches) == 1 and matches[0].get("owner_user_id") is None:
                profile_users.setdefault(matches[0]["key"], []).append(user)
        try:
            cutover_ms = int(self.settings_store.get_settings()["voicemail_associated_email_since_ms"])
        except (KeyError, TypeError, ValueError):
            # Fail closed if an incomplete migration cannot establish which
            # recordings predate automatic account-recipient delivery.
            cutover_ms = None
            logger.error("Voicemail associated-recipient cutover is missing or invalid")
        result: dict[str, tuple[str, int | None, int]] = {}
        for extension in rows:
            if extension["key"] not in live_keys or extension.get("voicemail_email_mode") == "off":
                continue
            if (extension.get("number") and str(extension.get("owner_user_id") or "")
                    != str(extension.get("number_owner_user_id") or "")):
                # A line in the middle of reassignment must not retain a former
                # tenant's custom address, either. Wait for ownership to agree.
                continue
            recipient = str(extension.get("voicemail_email") or "").strip()
            # A resumed opt-out or ownership change is new-only for all
            # recipients, including a deliberately configured custom address.
            resume_since_ms = int(extension.get("voicemail_email_resume_since_ms") or 0)
            auto_since_ms = resume_since_ms or None
            if not recipient:
                owner_id = extension.get("owner_user_id")
                if cutover_ms is None:
                    continue
                if owner_id is not None:
                    # Owner and number must name the same active customer.
                    if (not extension.get("number")
                            or str(extension.get("number_owner_user_id")) != str(owner_id)):
                        continue
                    account = users.get(str(owner_id))
                else:
                    # An ownerless legacy mailbox needs exactly one explicit,
                    # unambiguous user-profile assignment; never guess a tenant.
                    assigned_users = profile_users.get(extension["key"], [])
                    account = assigned_users[0] if len(assigned_users) == 1 else None
                recipient = str((account or {}).get("email") or "").strip()
                if not recipient:
                    continue
                auto_since_ms = max(cutover_ms, int(extension.get("voicemail_email_auto_since_ms") or 0), resume_since_ms)
            result[mailbox_name(extension["key"])] = (recipient, auto_since_ms, int(extension["id"]))
        return result

    def process(self) -> int:
        config = self.settings_store.get_email_config(include_key=True)
        if not config.get("enabled") or not config.get("api_key") or not config.get("from_email"):
            return 0
        recipients = self._mailbox_recipients()
        delivered = attempts = 0
        for candidate in self.voicemail_store.list_messages():
            if candidate["folder"] not in {"inbox", "urgent"}:
                continue
            target = recipients.get(candidate["mailbox"])
            if target is None:
                continue
            recipient, auto_since_ms, row_id = target
            try:
                snapshot = self._snapshot(candidate)
            except (OSError, ValueError):
                continue  # One removed/incomplete message must not stop the scan.
            if not snapshot:
                continue
            message, audio, audio_bytes, modified_ms = snapshot
            if auto_since_ms is not None and modified_ms < auto_since_ms:
                continue  # Older audio stays in the console; no surprise backlog.
            fingerprint = self._fingerprint(message, audio_bytes)
            if not self.settings_store.claim_voicemail_delivery(
                    fingerprint, message["mailbox"], recipient,
                    legacy_fingerprint=hashlib.sha256(message["mailbox"].encode() + audio_bytes).hexdigest(),
                    received_at=message.get("received_at")):
                continue
            transcript, transcription_status = self._transcribe(config, audio, audio_bytes)
            # Recognition can take a while, and one scan can process many
            # messages. Re-check opt-out, owner/recipient, flow and cutover just
            # before sending so an old scan does not leak a newly reassigned box.
            current = self._mailbox_recipients().get(message["mailbox"])
            current_config = self.settings_store.get_email_config(include_key=True)
            if (not current or current[0] != recipient or current[2] != row_id
                    or (current[1] is not None and modified_ms < current[1])
                    or not current_config.get("enabled") or not current_config.get("api_key")
                    or not current_config.get("from_email")):
                self.settings_store.finish_voicemail_delivery(
                    fingerprint, False, "Recipient or email settings changed before sending; no email was sent.",
                    transcription_status,
                )
                attempts += 1
                if attempts >= MAX_MESSAGES_PER_SCAN:
                    break
                continue
            if not current_config.get("transcription_enabled"):
                transcript, transcription_status = None, "disabled"
            success, error = self._send(current_config, self._payload(current_config, recipient, message, audio,
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
            return False, config.get("api_key_error") or "Save an Elastic Email API key before sending a test."
        if not config.get("from_email"):
            return False, "Save a verified Elastic Email sender email before sending a test."
        message = {"mailbox": "test", "caller_id": "EngineerIP test", "received_at": "now", "duration_seconds": 0, "message": "test"}
        payload = self._payload(config, recipient, message, None)
        payload["subject"] = "EngineerIP email delivery test"
        payload["text"] = ("Your EngineerIP email configuration is working. This test contains no voicemail audio "
                           f"and does not test transcription.\n\n{AUTOMATED_NOTICE}")
        payload["html"] = ("<p>Your EngineerIP email configuration is working.</p>"
                           "<p>This test contains no voicemail audio and does not test transcription.</p>"
                           f"<p>{html.escape(AUTOMATED_NOTICE)}</p>")
        return self._send(config, payload)
