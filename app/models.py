from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .database import Database


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


_COLUMNS = (
    "call_id", "contact_id", "member_id", "extension", "phone", "caller_id_number", "provider", "direction", "status",
    "answered", "started_at", "answered_at", "ended_at", "duration_seconds", "employee_channel_id",
    "customer_channel_id", "bridge_id", "recording_name", "recording_format", "recording_status",
    "recording_path", "disposition", "notes",
)


class CallStore:
    """SQLite-backed call state shared by the HTTP and ARI worker processes."""

    def __init__(self, path: str = "/app/instance/calls.db") -> None:
        uri = path if "://" in path else f"sqlite:///{path}"
        self.database = Database(uri)
        self.path = Path(path) if not self.database.is_mysql else None
        self._lock = Lock()
        self._init_db()

    def _connect(self):
        return self.database.connect()

    def _init_db(self) -> None:
        if self.database.is_mysql:
            self.database.create_all()
            return
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS calls (
                    call_id TEXT PRIMARY KEY,
                    contact_id TEXT,
                    member_id TEXT,
                    extension TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    caller_id_number TEXT,
                    provider TEXT,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    answered INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    answered_at TEXT,
                    ended_at TEXT,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    employee_channel_id TEXT,
                    customer_channel_id TEXT,
                    bridge_id TEXT,
                    recording_name TEXT,
                    recording_format TEXT,
                    recording_status TEXT,
                    recording_path TEXT,
                    disposition TEXT,
                    notes TEXT
                )
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(calls)").fetchall()}
            if "caller_id_number" not in columns:
                db.execute("ALTER TABLE calls ADD COLUMN caller_id_number TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS idx_calls_employee_channel ON calls(employee_channel_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_calls_customer_channel ON calls(customer_channel_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_calls_recording_name ON calls(recording_name)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_calls_started_at ON calls(started_at)")

    @staticmethod
    def _from_row(row: Any | None) -> Call | None:
        if row is None:
            return None
        data = dict(row)
        data["answered"] = bool(data["answered"])
        return Call(**data)

    def create(self, call: Call) -> Call:
        values = [getattr(call, col) for col in _COLUMNS]
        values[_COLUMNS.index("answered")] = int(call.answered)
        with self._lock, self._connect() as db:
            db.execute(
                f"INSERT INTO calls ({','.join(_COLUMNS)}) VALUES ({','.join('?' for _ in _COLUMNS)})",
                values,
            )
        return call

    def get(self, call_id: str) -> Call | None:
        with self._connect() as db:
            return self._from_row(db.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone())

    def find_by_channel(self, channel_id: str) -> Call | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM calls WHERE employee_channel_id=? OR customer_channel_id=? ORDER BY started_at DESC LIMIT 1",
                (channel_id, channel_id),
            ).fetchone()
            return self._from_row(row)

    def find_by_recording(self, recording_name: str) -> Call | None:
        with self._connect() as db:
            return self._from_row(db.execute("SELECT * FROM calls WHERE recording_name=? LIMIT 1", (recording_name,)).fetchone())

    def update(self, call_id: str, **changes: Any) -> Call | None:
        valid = {key: value for key, value in changes.items() if key in _COLUMNS and key != "call_id"}
        if not valid:
            return self.get(call_id)
        if "answered" in valid:
            valid["answered"] = int(bool(valid["answered"]))
        with self._lock, self._connect() as db:
            assignments = ",".join(f"{key}=?" for key in valid)
            values = list(valid.values()) + [call_id]
            db.execute(f"UPDATE calls SET {assignments} WHERE call_id=?", values)
            return self._from_row(db.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone())

    def all(self) -> list[Call]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM calls ORDER BY started_at DESC").fetchall()
        return [self._from_row(row) for row in rows if row is not None]

    def search(
        self,
        *,
        extension: str | None = None,
        extensions: list[str] | None = None,
        status: str | None = None,
        recordings_only: bool = False,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Call], int]:
        """Return a filtered, paginated call list and total result count."""
        clauses: list[str] = []
        values: list[Any] = []
        if extension:
            clauses.append("extension=?")
            values.append(extension)
        elif extensions is not None:
            if not extensions:
                clauses.append("1=0")
            else:
                clauses.append(f"extension IN ({','.join('?' for _ in extensions)})")
                values.extend(extensions)
        if status:
            clauses.append("status=?")
            values.append(status)
        if recordings_only:
            clauses.append("recording_name IS NOT NULL AND recording_status NOT IN ('deleted','failed')")
        if query:
            clauses.append("(phone LIKE ? OR call_id LIKE ? OR contact_id LIKE ? OR member_id LIKE ?)")
            pattern = f"%{query}%"
            values.extend([pattern, pattern, pattern, pattern])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as db:
            total = int(db.execute(f"SELECT COUNT(*) FROM calls{where}", values).fetchone()[0])
            rows = db.execute(
                f"SELECT * FROM calls{where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return [self._from_row(row) for row in rows if row is not None], total

    def summary(self, extension: str | None = None, extensions: list[str] | None = None) -> dict[str, int]:
        if extension:
            where, values = " WHERE extension=?", (extension,)
        elif extensions is not None:
            where = f" WHERE extension IN ({','.join('?' for _ in extensions)})" if extensions else " WHERE 1=0"
            values = tuple(extensions)
        else:
            where, values = "", ()
        with self._connect() as db:
            row = db.execute(f"""
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN answered=1 THEN 1 ELSE 0 END) AS answered,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN recording_status='finalized' THEN 1 ELSE 0 END) AS recordings
                FROM calls{where}
            """, values).fetchone()
        return {key: int(row[key] or 0) for key in ("total", "answered", "failed", "recordings")}
