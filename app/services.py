from __future__ import annotations

import hashlib
import threading
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
        self.store = store or CallStore()
        self.settings_store = settings_store
        self._finalizing: set[str] = set()
        self._finalize_lock = threading.Lock()

    def _provider_endpoint(self, provider_name: str | None) -> tuple[str, str]:
        if not self.settings_store:
            return "ipcomms", "IPComms"
        provider = self.settings_store.get_provider(provider_name)
        if not provider:
            raise RuntimeError("No active SIP provider is configured")
        endpoint = f"provider-{hashlib.sha256(provider['name'].encode()).hexdigest()[:12]}"
        return endpoint, provider["name"]

    def _recording_settings(self) -> dict[str, Any]:
        settings = self.settings_store.get_settings() if self.settings_store else {}
        fmt = str(settings.get("recording_format", "wav")).lower().strip()
        if fmt not in {"wav", "wav49", "gsm", "slin16"}:
            fmt = "wav"
        try:
            max_duration = max(0, int(settings.get("recording_max_duration_seconds", "0")))
        except (TypeError, ValueError):
            max_duration = 0
        try:
            retention = max(0, int(settings.get("recording_retention_days", "90")))
        except (TypeError, ValueError):
            retention = 90
        return {
            "enabled": _bool_setting(settings, "recording_enabled", True),
            "format": fmt,
            "beep": _bool_setting(settings, "recording_beep", False),
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
        provider_endpoint, provider_name = self._provider_endpoint(provider)
        call_id = self.asterisk.create_outbound_call(
            extension,
            phone,
            provider_endpoint,
            {"contact_id": contact_id, "member_id": member_id},
        )
        call = Call(
            call_id=call_id,
            contact_id=str(contact_id) if contact_id is not None else None,
            member_id=str(member_id) if member_id is not None else None,
            extension=str(extension),
            phone=phone,
            provider=provider_name,
            employee_channel_id=f"{call_id}-employee",
            status="ringing",
        )
        self.store.create(call)
        self.notify_crm("call.started", call)
        self.notify_crm("call.ringing", call)
        return call

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
            self.notify_crm("call.failed", updated)

    def _start_bridge(self, call: Call) -> None:
        if call.bridge_id or not call.employee_channel_id or not call.customer_channel_id:
            return
        bridge_id = self.asterisk.create_bridge(call.call_id)
        self.asterisk.add_channel_to_bridge(bridge_id, call.employee_channel_id)
        self.asterisk.add_channel_to_bridge(bridge_id, call.customer_channel_id)
        updated = self.store.update(call.call_id, bridge_id=bridge_id, status="bridged")
        self.notify_crm("call.bridged", updated)

        recording = self._recording_settings()
        if not recording["enabled"]:
            return
        name = f"call-{call.call_id}"
        try:
            self.asterisk.start_bridge_recording(
                bridge_id,
                name,
                recording["format"],
                recording["beep"],
                recording["max_duration"],
            )
            updated = self.store.update(
                call.call_id,
                recording_name=name,
                recording_format=recording["format"],
                recording_status="recording",
            )
            self.notify_crm("call.recording_started", updated)
        except Exception:
            updated = self.store.update(call.call_id, recording_status="failed")
            self.notify_crm("call.recording_failed", updated)

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
            try:
                started = datetime.fromisoformat(call.started_at)
                duration = max(0, int((datetime.fromisoformat(ended) - started).total_seconds()))
            except ValueError:
                duration = 0
            if call.recording_name:
                self.asterisk.stop_recording(call.recording_name)
                recording_path = str(Path(self.config.RECORDING_VOLUME_PATH) / f"{call.recording_name}.{call.recording_format or 'wav'}")
                recording_status = "finalized"
            else:
                recording_path = None
                recording_status = call.recording_status
            if call.bridge_id:
                self.asterisk.destroy_bridge(call.bridge_id)
            updated = self.store.update(
                call_id,
                status="completed",
                ended_at=ended,
                duration_seconds=duration,
                recording_status=recording_status,
                recording_path=recording_path,
            )
            self.notify_crm("call.completed", updated, {"reason": reason})
            self.cleanup_recordings()
        finally:
            with self._finalize_lock:
                self._finalizing.discard(call_id)

    def cleanup_recordings(self) -> None:
        days = self._recording_settings()["retention_days"]
        if days <= 0:
            return
        root = Path(self.config.RECORDING_VOLUME_PATH)
        if not root.exists():
            return
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        for path in root.glob("call-*.*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def handle_ari_event(self, event: dict[str, Any]) -> None:
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        if not channel_id:
            return
        call = self.store.find_by_channel(channel_id)
        if not call:
            return

        event_type = event.get("type")
        if event_type == "StasisStart":
            if channel_id == call.employee_channel_id:
                updated = self.store.update(call.call_id, status="ringing")
                self.notify_crm("call.employee_ringing", updated)
            return

        if event_type == "ChannelStateChange":
            state = str(channel.get("state", "")).lower()
            if state != "up":
                return
            if channel_id == call.employee_channel_id and not call.customer_channel_id:
                updated = self.store.update(call.call_id, status="employee_answered")
                self.notify_crm("call.employee_answered", updated)
                self._start_customer(updated)
            elif channel_id == call.customer_channel_id:
                self._start_bridge(call)
                updated = self.store.update(call.call_id, status="answered", answered=True, answered_at=iso_now())
                self.notify_crm("call.answered", updated)
            return

        if event_type == "RecordingFinished":
            recording = event.get("recording") or {}
            if recording.get("name") == call.recording_name:
                updated = self.store.update(call.call_id, recording_status="finalized")
                self.notify_crm("call.recording_finished", updated)
            return

        if event_type == "ChannelDestroyed":
            other = call.customer_channel_id if channel_id == call.employee_channel_id else call.employee_channel_id
            if other:
                self.asterisk.hangup(other)
            self._finalize(call.call_id, "channel_destroyed")
