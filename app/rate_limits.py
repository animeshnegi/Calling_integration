"""Database-backed, atomic fixed-window limits shared by all application workers."""
import hashlib
import time

from sqlalchemy import BigInteger, Column, Index, Integer, String, Table

from .database import metadata

rate_limits = Table("rate_limits", metadata,
      Column("bucket", String(64), primary_key=True),
      Column("window_start", BigInteger, nullable=False),
      Column("attempts", Integer, nullable=False))
Index("idx_rate_limits_window", rate_limits.c.window_start)


class RateLimiter:
    def __init__(self, database):
        self.database = database
        if not database.is_mysql:
            with database.connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS rate_limits (bucket TEXT PRIMARY KEY, window_start INTEGER NOT NULL, attempts INTEGER NOT NULL)")
                db.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_window ON rate_limits(window_start)")

    def limited(self, key, limit, seconds=60):
        now = int(time.time())
        window_start = now // seconds * seconds
        bucket = hashlib.sha256(f"{seconds}:{key}".encode()).hexdigest()
        with self.database.connect() as db:
            db.execute("DELETE FROM rate_limits WHERE window_start<?", (now - 86400,))
            if self.database.is_mysql:
                db.execute("INSERT INTO rate_limits(bucket,window_start,attempts) VALUES(?,?,1) ON DUPLICATE KEY UPDATE attempts=IF(window_start=VALUES(window_start),attempts+1,1),window_start=VALUES(window_start)", (bucket, window_start))
            else:
                db.execute("INSERT INTO rate_limits(bucket,window_start,attempts) VALUES(?,?,1) ON CONFLICT(bucket) DO UPDATE SET attempts=CASE WHEN window_start=excluded.window_start THEN attempts+1 ELSE 1 END,window_start=excluded.window_start", (bucket, window_start))
            count = db.execute("SELECT attempts FROM rate_limits WHERE bucket=?", (bucket,)).fetchone()[0]
        return count > limit
