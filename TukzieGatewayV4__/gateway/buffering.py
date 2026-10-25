"""
Store-and-forward buffering (Task 10, Day 3).

Broker-agnostic: PublishAttemptFn is any callable that tries to publish
and raises on failure. StoreAndForwardBuffer doesn't know or care if
that's talking to local_docker or hostinger_tukzie.

Usage:
    buf = StoreAndForwardBuffer(publish_fn=lambda payload: conn.client.publish(
        conn.topic("telemetry"), payload, qos=1
    ))
    buf.send(telemetry_packet.to_dict())   # enqueues if publish fails
    ...
    buf.flush()   # call after reconnecting
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger("tukzie.buffering")

PublishAttemptFn = Callable[[dict], None]  # raises on failure, returns None on success


class StoreAndForwardBuffer:
    def __init__(self, publish_fn: PublishAttemptFn, db_path: str = "buffer.sqlite3"):
        self.publish_fn = publish_fn
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    flushed INTEGER NOT NULL DEFAULT 0
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # ---- writing ----------------------------------------------------

    def send(self, payload: dict) -> bool:
        """Try to publish live. On failure, enqueue instead of dropping.
        Returns True if it went out live, False if it was queued."""
        try:
            self.publish_fn(payload)
            return True
        except Exception as e:
            logger.warning("publish failed (%s), enqueueing payload", e)
            self._enqueue(payload)
            return False

    def _enqueue(self, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO queue (payload, created_at, flushed) VALUES (?, ?, 0)",
                (json.dumps(payload), time.time()),
            )

    # ---- flushing -----------------------------------------------------

    def flush(self) -> int:
        """Publish everything queued, in original order. Marks each row
        flushed only after a successful publish, so a crash mid-flush
        doesn't lose or duplicate silently on the next run.
        Returns count successfully flushed."""
        flushed_count = 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, payload FROM queue WHERE flushed = 0 ORDER BY id ASC"
            ).fetchall()

            for row_id, payload_json in rows:
                payload = json.loads(payload_json)
                try:
                    self.publish_fn(payload)
                except Exception as e:
                    logger.error("flush stopped at row %s: %s", row_id, e)
                    break  # stop here, preserve order — don't skip ahead
                conn.execute("UPDATE queue SET flushed = 1 WHERE id = ?", (row_id,))
                conn.commit()
                flushed_count += 1

        logger.info("flushed %d queued payload(s)", flushed_count)
        return flushed_count

    def pending_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM queue WHERE flushed = 0").fetchone()[0]
