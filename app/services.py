from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .config import Config
from .models import Call, CallStore


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool_setting(settings: dict[str, str], key: str, default: bool) -> bool:
    value = settings.get(key)
    if value is None:
        return default
    return str(value).lower() in {"1", "true", "yes", "on"}


class TelephonyService:
    def __init__(self, asterisk, config: type[Config] = Config, store: CallStore | None = None, settings_store=None):
        self.asterisk = asterisk
        self.config = config
        self.store = store or CallStore(config.CALLS_DB_PATH)
        self.settings_store = settings_store
        self._finalizing: set[str] = set()
        self._finalize_lock = threading.Lock()

    def ari_ready(self) -> bool:
        try:
            ready = Path(self.config.ARI_READY_PATH)
            return ready.exists() and (datetime.now(timezone.utc).timestamp() - ready.stat().st_mtime) <= 15
        except OSError:
            return False

    def _provider_endpoint(self, provider_name: str | None) -> tuple[str, str]:
        if not self.settings_store:
            return "ipcomms", "IPComms"
        provider = self.settings_store.get_provider(provider_name)
        if not provider:
            raise RuntimeError("No active SIP provider is configured")
        endpoint = f"provider-{hashlib.sha256(provider['name'].encode()).hexdigest()[:12]}"
        return endpoint, provider["name"]

    def _recording_settings(self, extension: str | None = None) -> dict[str, Any]:
        settings = self.settings_store.get_settings() if self.settings_store else {}
        fmt = str(settings.get("recording_format", "wav")).lower().strip()
        if fmt not in {"wav", "wav49", "gsm", "slin16"}:
            fmt = "wav"
        try:
            max_duration = max(0, int(settings.get("recording_max_duration_seconds", "0")))
        except (TypeError, ValueError):
            max_duration = 0
        try:
            retention = max(1, int(settings.get("recording_retention_days", "90")))
        except (TypeError, ValueError):
            retention = 90
        extension_enabled = True
        if extension and self.settings_store:
            row = next((item for item in self.settings_store.list_extensions() if item["extension"] == extension), None)
            extension_enabled = bool(row and row["active"] and row["recording_enabled"])
        return {
            "enabled": _bool_setting(settings, "recording_enabled", True) and extension_enabled,
            "format": fmt,
            "beep": _bool_setting(settings, "recording_beep", False),
            "announcement": _bool_setting(settings, "recording_announcement", False),
            "announcement_media": str(settings.get("recording_announcement_media", "")).strip(),
            "retention_days": retention,
            "max_duration": max_duration,
        }

    def notify_crm(self, event: str, call: Call | None, extra: dict[str, Any] | None = None) -> None:
        if call is None or not self.config.CRM_WEBHOOK_URL:
            return
        payload = {"event": event, "call": call.to_dict()}
        if extra:
            payload.update(extra)
        headers = {"Content-Type": "application/json"}
        if self.config.CRM_WEBHOOK_TOKEN:
            headers["Authorization"] = f"Bearer {self.config.CRM_WEBHOOK_TOKEN}"
        try:
            requests.post(self.config.CRM_WEBHOOK_URL, json=payload, headers=headers, timeout=5)
        except requests.RequestException:
            pass

    def start_outbound(self, *, phone: str, extension: str, contact_id=None, member_id=None, provider=None) -> Call:
        if not self.ari_ready():
            raise RuntimeError("ARI event worker is not ready")
        provider_endpoint, provider_name = self._provider_endpoint(provider)
        call_id = str(uuid.uuid4())
        call = Call(
            call_id=call_id,
            contact_id=str(contact_id) if contact_id is not None else None,
            member_id=str(member_id) if member_id is not None else None,
            extension=str(extension),
            phone=phone,
            provider=provider_name,
            employee_channel_id=f"{call_id}-employee",
            status="initiated",
        )
        # Persist the state before originating. ARI can emit StasisStart immediately.
        self.store.create(call)
        try:
            self.asterisk.create_outbound_call(
                call_id,
                extension,
                phone,
                provider_endpoint,
                {"contact_id": contact_id, "member_id": member_id},
            )
        except Exception:
            updated = self.store.update(call_id, status="failed", ended_at=iso_now())
            self.notify_crm("call.failed", updated, {"reason": "asterisk_originate_failed"})
            raise
        updated = self.store.update(call_id, status="ringing")
        self.notify_crm("call.started", updated)
        self.notify_crm("call.ringing", updated)
        return updated

    def hangup(self, call_id: str) -> Call | None:
        call = self.store.get(call_id)
        if not call:
            return None
        self.asterisk.hangup_call(call.employee_channel_id, call.customer_channel_id)
        self._finalize(call_id, "hangup_requested")
        return self.store.get(call_id)

    def _start_customer(self, call: Call) -> None:
        if call.customer_channel_id or call.status in {"completed", "failed"}:
            return
        endpoint, _ = self._provider_endpoint(call.provider)
        try:
            customer_channel = self.asterisk.create_customer_leg(
                call.call_id, call.phone, endpoint, call.employee_channel_id or ""
            )
            updated = self.store.update(call.call_id, customer_channel_id=customer_channel, status="dialing_customer")
            self.notify_crm("call.customer_dialing", updated)
        except Exception:
            self.asterisk.hangup(call.employee_channel_id or "")
            updated = self.store.update(call.call_id, status="failed", ended_at=iso_now())
            self.notify_crm("call.failed", updated, {"reason": "customer_originate_failed"})

    def _start_bridge(self, call: Call) -> None:
        current = self.store.get(call.call_id)
        if not current or current.bridge_id or not current.employee_channel_id or not current.customer_channel_id:
            return
        bridge_id = None
        try:
            bridge_id = self.asterisk.create_bridge(current.call_id)
            self.asterisk.add_channel_to_bridge(bridge_id, current.employee_channel_id)
            self.asterisk.add_channel_to_bridge(bridge_id, current.customer_channel_id)
            updated = self.store.update(current.call_id, bridge_id=bridge_id, status="bridged")
            self.notify_crm("call.bridged", updated)
            recording = self._recording_settings(updated.extension)
            if not recording["enabled"]:
                return
            name = f"call-{updated.call_id}"
            self.asterisk.start_bridge_recording(
                bridge_id,
                name,
                recording["format"],
                recording["beep"],
                recording["max_duration"],
            )
            updated = self.store.update(
                updated.call_id,
                recording_name=name,
                recording_format=recording["format"],
                recording_status="recording",
            )
            self.notify_crm("call.recording_started", updated)
            if recording["announcement"] and recording["announcement_media"]:
                try:
                    self.asterisk.play_bridge_media(bridge_id, recording["announcement_media"])
                except Exception:
                    self.notify_crm("call.recording_announcement_failed", updated)
        except Exception:
            if bridge_id:
                self.asterisk.destroy_bridge(bridge_id)
            self.asterisk.hangup(current.employee_channel_id or "")
            self.asterisk.hangup(current.customer_channel_id or "")
            updated = self.store.update(
                current.call_id,
                status="failed",
                ended_at=iso_now(),
                bridge_id=None,
                recording_status="failed",
            )
            self.notify_crm("call.failed", updated, {"reason": "bridge_setup_failed"})

    def _finalize(self, call_id: str, reason: str) -> None:
        with self._finalize_lock:
            if call_id in self._finalizing:
                return
            self._finalizing.add(call_id)
        try:
            call = self.store.get(call_id)
            if not call or call.status == "completed":
                return
            ended = iso_now()
            duration_start = call.answered_at or call.started_at
            try:
                started = datetime.fromisoformat(duration_start)
                duration = max(0, int((datetime.fromisoformat(ended) - started).total_seconds()))
            except (ValueError, TypeError):
                duration = 0
            recording_status = call.recording_status
            if call.recording_name and recording_status == "recording":
                self.asterisk.stop_recording(call.recording_name)
                recording_status = "finalizing"
            if call.bridge_id:
                self.asterisk.destroy_bridge(call.bridge_id)
            terminal_status = "failed" if call.status == "failed" else ("completed" if call.answered else "failed")
            updated = self.store.update(
                call_id,
                status=terminal_status,
                ended_at=ended,
                duration_seconds=duration,
                recording_status=recording_status,
            )
            if terminal_status == "completed":
                self.notify_crm("call.completed", updated, {"reason": reason})
            else:
                self.notify_crm(
                    "call.failed",
                    updated,
                    {"reason": "call_ended_before_answer" if reason == "channel_destroyed" else reason},
                )
        finally:
            with self._finalize_lock:
                self._finalizing.discard(call_id)

    def cleanup_recordings(self) -> None:
        days = self._recording_settings()["retention_days"]
        self.asterisk.cleanup_old_recordings(days)

    def handle_ari_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")

        if event_type == "RecordingFinished":
            recording = event.get("recording") or {}
            name = str(recording.get("name") or "")
            call = self.store.find_by_recording(name) if name else None
            if call:
                stored = self.asterisk.get_stored_recording(name) if name else None
                changes: dict[str, Any] = {"recording_status": "finalized"}
                if stored:
                    filename = stored.get("filename")
                    if filename:
                        changes["recording_path"] = str(filename)
                    if stored.get("format") and not call.recording_format:
                        changes["recording_format"] = str(stored["format"])
                updated = self.store.update(call.call_id, **changes)
                self.notify_crm("call.recording_finished", updated)
            return

        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        if not channel_id:
            return
        call = self.store.find_by_channel(channel_id)
        if not call:
            return

        if event_type == "StasisStart":
            if channel_id == call.employee_channel_id:
                updated = self.store.update(call.call_id, status="ringing")
                self.notify_crm("call.employee_ringing", updated)
            return

        if event_type == "ChannelStateChange":
            if str(channel.get("state", "")).lower() != "up":
                return
            current = self.store.get(call.call_id)
            if not current:
                return
            if channel_id == current.employee_channel_id and not current.customer_channel_id:
                updated = self.store.update(current.call_id, status="employee_answered")
                self.notify_crm("call.employee_answered", updated)
                self._start_customer(updated)
            elif channel_id == current.customer_channel_id:
                self._start_bridge(current)
                updated = self.store.get(current.call_id)
                if updated and updated.status != "failed" and not updated.answered:
                    updated = self.store.update(
                        updated.call_id,
                        status="answered",
                        answered=True,
                        answered_at=iso_now(),
                    )
                    self.notify_crm("call.answered", updated)
            return

        if event_type == "ChannelDestroyed":
            other = call.customer_channel_id if channel_id == call.employee_channel_id else call.employee_channel_id
            if other:
                self.asterisk.hangup(other)
            self._finalize(call.call_id, "channel_destroyed")

    def recover_incomplete_calls(self) -> None:
        """Fail orphaned records only when Asterisk no longer has either channel."""
        try:
            live_ids = {str(channel.get("id")) for channel in self.asterisk.list_channels() if channel.get("id")}
        except Exception:
            return
        for call in self.store.all():
            if call.status in {"completed", "failed"} or call.ended_at is not None:
                continue
            if call.employee_channel_id in live_ids or call.customer_channel_id in live_ids:
                continue
            updated = self.store.update(call.call_id, status="failed", ended_at=iso_now())
            self.notify_crm("call.failed", updated, {"reason": "worker_restart_no_live_channel"})
