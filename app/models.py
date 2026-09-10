from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any


@dataclass
class Call:
    call_id: str
    contact_id: str | None
    member_id: str | None
    extension: str
    phone: str
    direction: str = "outbound"
    status: str = "initiated"
    answered: bool = False
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    answered_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int = 0
    disposition: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CallStore:
    def __init__(self) -> None:
        self._calls: dict[str, Call] = {}
        self._lock = Lock()

    def create(self, call: Call) -> Call:
        with self._lock:
            self._calls[call.call_id] = call
        return call

    def get(self, call_id: str) -> Call | None:
        return self._calls.get(call_id)

    def update(self, call_id: str, **changes: Any) -> Call | None:
        with self._lock:
            call = self._calls.get(call_id)
            if not call:
                return None
            for key, value in changes.items():
                if hasattr(call, key):
                    setattr(call, key, value)
            return call

    def all(self) -> list[Call]:
        with self._lock:
            return list(self._calls.values())
