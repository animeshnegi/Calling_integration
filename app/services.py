from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from .config import Config
from .models import Call, CallStore


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TelephonyService:
    def __init__(self, asterisk, config: type[Config] = Config, store: CallStore | None = None):
        self.asterisk = asterisk
        self.config = config
        self.store = store or CallStore()

    def notify_crm(self, event: str, call: Call, extra: dict[str, Any] | None = None) -> None:
        url = self.config.CRM_WEBHOOK_URL
        if not url:
            return
        payload = {"event": event, "call": call.to_dict()}
        if extra:
            payload.update(extra)
        headers = {"Content-Type": "application/json"}
        if self.config.CRM_WEBHOOK_TOKEN:
            headers["Authorization"] = f"Bearer {self.config.CRM_WEBHOOK_TOKEN}"
        try:
            requests.post(url, json=payload, headers=headers, timeout=5)
        except requests.RequestException:
            # Call state remains authoritative locally; webhook delivery can be retried later.
            pass

    def start_outbound(self, *, phone: str, extension: str, contact_id=None, member_id=None) -> Call:
        call_id = self.asterisk.create_outbound_call(
            extension,
            phone,
            {"contact_id": contact_id, "member_id": member_id},
        )
        call = Call(
            call_id=call_id,
            contact_id=str(contact_id) if contact_id is not None else None,
            member_id=str(member_id) if member_id is not None else None,
            extension=str(extension),
            phone=phone,
        )
        self.store.create(call)
        self.notify_crm("call.started", call)
        return call

    def hangup(self, call_id: str) -> Call | None:
        call = self.store.get(call_id)
        if not call:
            return None
        self.asterisk.hangup(call_id)
        updated = self.store.update(
            call_id,
            status="completed",
            ended_at=iso_now(),
            duration_seconds=0 if not call.started_at else call.duration_seconds,
        )
        if updated:
            self.notify_crm("call.hangup_requested", updated)
        return updated

    def handle_ari_event(self, event: dict[str, Any]) -> None:
        # Filter only channels owned by this integration.
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        call_id = channel_id
        if not call_id:
            return
        call = self.store.get(call_id)
        if not call:
            return

        event_type = event.get("type")
        if event_type == "ChannelStateChange":
            state = channel.get("state", "").lower()
            if state == "ringing":
                self.store.update(call_id, status="ringing")
                self.notify_crm("call.ringing", self.store.get(call_id))
            elif state == "up":
                updated = self.store.update(call_id, status="answered", answered=True, answered_at=iso_now())
                self.notify_crm("call.answered", updated)
        elif event_type == "ChannelDestroyed":
            ended = iso_now()
            started = datetime.fromisoformat(call.started_at)
            duration = max(0, int((datetime.fromisoformat(ended) - started).total_seconds()))
            updated = self.store.update(
                call_id,
                status="completed",
                ended_at=ended,
                duration_seconds=duration,
            )
            if updated:
                self.notify_crm("call.completed", updated)
        elif event_type == "StasisStart":
            updated = self.store.update(call_id, status="in_progress")
            self.notify_crm("call.in_progress", updated)
