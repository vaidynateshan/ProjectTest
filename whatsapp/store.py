"""SQLite persistence for conversations.

A connection is opened per operation. SQLite handles that well, it keeps the
store safe to share between the webhook's threadpool and the MCP server's
event loop, and it avoids a long-lived handle that breaks after a redeploy.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from contextlib import contextmanager

from .models import InboundMessage, StatusUpdate

#: Meta only allows freeform (non-template) messages within 24 hours of the
#: customer's most recent message. Outside it, sends fail with error 131047.
CUSTOMER_SERVICE_WINDOW_SECONDS = 24 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    wa_id         TEXT PRIMARY KEY,
    profile_name  TEXT,
    first_seen_at INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    wa_id      TEXT NOT NULL,
    direction  TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    msg_type   TEXT NOT NULL,
    text       TEXT,
    media_id   TEXT,
    mime_type  TEXT,
    filename   TEXT,
    reply_to   TEXT,
    status     TEXT,
    error      TEXT,
    timestamp  INTEGER NOT NULL,
    raw        TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages (wa_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages (timestamp);
"""


@dataclass
class ThreadSummary:
    wa_id: str
    profile_name: str | None
    message_count: int
    last_message_at: int
    last_message_preview: str | None
    last_inbound_at: int | None

    @property
    def window_open(self) -> bool:
        """Whether a freeform reply is currently allowed for this thread."""
        if self.last_inbound_at is None:
            return False
        return (time.time() - self.last_inbound_at) < CUSTOMER_SERVICE_WINDOW_SECONDS

    @property
    def window_expires_at(self) -> int | None:
        if self.last_inbound_at is None:
            return None
        return self.last_inbound_at + CUSTOMER_SERVICE_WINDOW_SECONDS


class ConversationStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if self.db_path.parent != Path(""):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            # WAL lets the MCP server read while the webhook is writing.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    # -- writes ---------------------------------------------------------

    def upsert_contact(self, wa_id: str, profile_name: str | None, seen_at: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO contacts (wa_id, profile_name, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(wa_id) DO UPDATE SET
                    last_seen_at = MAX(last_seen_at, excluded.last_seen_at),
                    -- never overwrite a known name with a missing one
                    profile_name = COALESCE(excluded.profile_name, profile_name)
                """,
                (wa_id, profile_name, seen_at, seen_at),
            )

    def save_inbound(self, message: InboundMessage) -> bool:
        """Persist an inbound message. Returns False if already stored.

        Meta redelivers webhooks on any non-200, so this must be idempotent.
        """
        if message.profile_name or message.wa_id:
            self.upsert_contact(message.wa_id, message.profile_name, message.timestamp)

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (id, wa_id, direction, msg_type, text, media_id, mime_type,
                     filename, reply_to, status, error, timestamp, raw, created_at)
                VALUES (?, ?, 'in', ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    message.id,
                    message.wa_id,
                    message.msg_type,
                    message.text,
                    message.media_id,
                    message.mime_type,
                    message.filename,
                    message.reply_to,
                    message.timestamp,
                    message.raw_json,
                    int(time.time()),
                ),
            )
            return cursor.rowcount > 0

    def save_outbound(
        self,
        message_id: str,
        wa_id: str,
        msg_type: str,
        text: str | None,
        *,
        reply_to: str | None = None,
        media_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        """Record a message we just sent, so threads show both sides."""
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (id, wa_id, direction, msg_type, text, media_id, mime_type,
                     filename, reply_to, status, error, timestamp, raw, created_at)
                VALUES (?, ?, 'out', ?, ?, ?, NULL, NULL, ?, 'accepted', NULL, ?, ?, ?)
                """,
                (
                    message_id,
                    wa_id,
                    msg_type,
                    text,
                    media_id,
                    reply_to,
                    now,
                    json.dumps(raw or {}, separators=(",", ":")),
                    now,
                ),
            )

    def record_status(self, status: StatusUpdate) -> None:
        """Apply a delivery receipt.

        Inserts a placeholder row when the message is unknown -- that happens
        for messages a colleague sent from the WhatsApp Manager UI rather
        than through this bridge.
        """
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE messages
                   SET status = ?,
                       error = COALESCE(?, error)
                 WHERE id = ?
                """,
                (status.status, status.error, status.message_id),
            )
            if updated.rowcount == 0:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (id, wa_id, direction, msg_type, text, media_id, mime_type,
                         filename, reply_to, status, error, timestamp, raw, created_at)
                    VALUES (?, ?, 'out', 'unknown', NULL, NULL, NULL,
                            NULL, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        status.message_id,
                        status.wa_id,
                        status.status,
                        status.error,
                        status.timestamp,
                        status.raw_json,
                        int(time.time()),
                    ),
                )

    # -- reads ----------------------------------------------------------

    def list_threads(self, limit: int = 20) -> list[ThreadSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.wa_id,
                       c.profile_name                                    AS profile_name,
                       COUNT(*)                                          AS message_count,
                       MAX(m.timestamp)                                  AS last_message_at,
                       MAX(CASE WHEN m.direction = 'in' THEN m.timestamp END)
                                                                         AS last_inbound_at
                  FROM messages m
                  LEFT JOIN contacts c ON c.wa_id = m.wa_id
                 GROUP BY m.wa_id
                 ORDER BY last_message_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()

            summaries = []
            for row in rows:
                preview = conn.execute(
                    """
                    SELECT text, msg_type FROM messages
                     WHERE wa_id = ? ORDER BY timestamp DESC, rowid DESC LIMIT 1
                    """,
                    (row["wa_id"],),
                ).fetchone()
                summaries.append(
                    ThreadSummary(
                        wa_id=row["wa_id"],
                        profile_name=row["profile_name"],
                        message_count=row["message_count"],
                        last_message_at=row["last_message_at"],
                        last_message_preview=(
                            preview["text"] or f"<{preview['msg_type']}>"
                            if preview
                            else None
                        ),
                        last_inbound_at=row["last_inbound_at"],
                    )
                )
            return summaries

    def read_thread(self, wa_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` messages, oldest first."""
        with self._connect() as conn:
            # rowid breaks ties in both directions: several messages routinely
            # share a timestamp (Meta reports whole seconds), and without a
            # tiebreaker on the outer sort their order is undefined.
            rows = conn.execute(
                """
                SELECT id, wa_id, direction, msg_type, text, media_id, mime_type,
                       filename, reply_to, status, error, timestamp
                  FROM (
                    SELECT rowid AS rid, id, wa_id, direction, msg_type, text,
                           media_id, mime_type, filename, reply_to, status,
                           error, timestamp
                      FROM messages
                     WHERE wa_id = ?
                     ORDER BY timestamp DESC, rowid DESC
                     LIMIT ?
                ) ORDER BY timestamp ASC, rid ASC
                """,
                (wa_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def search_messages(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.wa_id, c.profile_name, m.direction, m.msg_type,
                       m.text, m.timestamp
                  FROM messages m
                  LEFT JOIN contacts c ON c.wa_id = m.wa_id
                 WHERE m.text LIKE ? ESCAPE '\\'
                 ORDER BY m.timestamp DESC
                 LIMIT ?
                """,
                (f"%{_escape_like(query)}%", limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def thread(self, wa_id: str) -> ThreadSummary | None:
        """Summary for a single thread, including its 24-hour window state."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT m.wa_id,
                       c.profile_name                                    AS profile_name,
                       COUNT(*)                                          AS message_count,
                       MAX(m.timestamp)                                  AS last_message_at,
                       MAX(CASE WHEN m.direction = 'in' THEN m.timestamp END)
                                                                         AS last_inbound_at
                  FROM messages m
                  LEFT JOIN contacts c ON c.wa_id = m.wa_id
                 WHERE m.wa_id = ?
                 GROUP BY m.wa_id
                """,
                (wa_id,),
            ).fetchone()
            if row is None:
                return None

            preview = conn.execute(
                """
                SELECT text, msg_type FROM messages
                 WHERE wa_id = ? ORDER BY timestamp DESC, rowid DESC LIMIT 1
                """,
                (wa_id,),
            ).fetchone()

        return ThreadSummary(
            wa_id=row["wa_id"],
            profile_name=row["profile_name"],
            message_count=row["message_count"],
            last_message_at=row["last_message_at"],
            last_message_preview=(
                preview["text"] or f"<{preview['msg_type']}>" if preview else None
            ),
            last_inbound_at=row["last_inbound_at"],
        )

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            return dict(row) if row else None


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so a literal % or _ in a query does not match all."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
