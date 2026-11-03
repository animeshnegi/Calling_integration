"""The persisted call record."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Call:
    call_id: str
    contact_id: str | None
    member_id: str | None
    extension: str
    phone: str
    caller_id_number: str | None = None
    provider: str | None = None
    direction: str = "outbound"
    status: str = "initiated"
    answered: bool = False
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    answered_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int = 0
    employee_channel_id: str | None = None
    # Every leg rung for this call, comma separated. A primary number rings the
    # customer's devices together, so more than one exists until somebody answers.
    employee_channel_ids: str = ""
    customer_channel_id: str | None = None
    bridge_id: str | None = None
    recording_name: str | None = None
    recording_format: str | None = None
    recording_status: str | None = None
    recording_path: str | None = None
    disposition: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



__all__ = ["Call"]
