"""Durable call reservations. Call primary keys fence late/crashed request owners."""
import hashlib
import json
import time
import uuid

from sqlalchemy import BigInteger, Column, String, Table

from .database import metadata

Table("call_requests", metadata,
      Column("client", String(100), primary_key=True),
      Column("request_key", String(128), primary_key=True),
      Column("fingerprint", String(64), nullable=False),
      Column("call_id", String(128), nullable=False),
      Column("lease_until", BigInteger, nullable=False))


def reserve(database, client, key, data):
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    call_id = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["eip-call", client, key], separators=(",", ":"))))
    now = int(time.time())
    # Preserve the previous implementation's live reservations during upgrades.
    if "call_id" in database.columns("api_idempotency"):
        with database.connect() as db:
            legacy = db.execute("SELECT call_id FROM api_idempotency WHERE client=? AND request_key=? AND created_at>=datetime('now','-24 hours')", (client, key)).fetchone()
        if legacy:
            if not legacy["call_id"]:
                raise ValueError("Legacy call reservation needs reconciliation before retry")
            call_id = legacy["call_id"]
    with database.connect() as db:
        if not database.is_mysql:
            db.execute("CREATE TABLE IF NOT EXISTS call_requests (client TEXT NOT NULL,request_key TEXT NOT NULL,fingerprint TEXT NOT NULL,call_id TEXT NOT NULL,lease_until INTEGER NOT NULL,PRIMARY KEY(client,request_key))")
        db.execute("INSERT OR IGNORE INTO call_requests(client,request_key,fingerprint,call_id,lease_until) VALUES(?,?,?,?,0)",
                   (client, key, fingerprint, call_id))
        row = db.execute("SELECT fingerprint,call_id FROM call_requests WHERE client=? AND request_key=?", (client, key)).fetchone()
        if row["fingerprint"] != fingerprint:
            raise ValueError("Idempotency-Key was already used with different input")
        result = db.execute("UPDATE call_requests SET lease_until=? WHERE client=? AND request_key=? AND lease_until<=?",
                            (now + 60, client, key, now))
        return result.rowcount == 1, row["call_id"]
