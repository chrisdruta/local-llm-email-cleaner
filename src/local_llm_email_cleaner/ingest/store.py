"""Idempotent batched insertion of parsed messages into SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..models import ParsedMessage
from .mbox_reader import iter_mbox

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

# INSERT OR IGNORE -> re-running ingest is idempotent: duplicate gmail_msgid /
# rfc_message_id rows are silently skipped.
_INSERT_SQL = """
INSERT OR IGNORE INTO messages (
    gmail_msgid, thread_id, rfc_message_id, labels, date_utc, date_epoch,
    from_addr, from_name, from_domain, to_addr, to_all, subject,
    body_text, has_attachments, attachment_names, size_bytes, list_unsubscribe
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass
class IngestStats:
    seen: int = 0
    inserted: int = 0

    @property
    def skipped(self) -> int:
        return self.seen - self.inserted


def _row(msg: ParsedMessage) -> tuple:
    return (
        msg.gmail_msgid,
        msg.thread_id,
        msg.rfc_message_id,
        msg.labels,
        msg.date_utc,
        msg.date_epoch,
        msg.from_addr,
        msg.from_name,
        msg.from_domain,
        msg.to_addr,
        msg.to_all,
        msg.subject,
        msg.body_text,
        int(msg.has_attachments),
        json.dumps(msg.attachment_names),
        msg.size_bytes,
        int(msg.list_unsubscribe),
    )


def insert_messages(
    conn: sqlite3.Connection,
    messages: Iterable[ParsedMessage],
    progress: Callable[[IngestStats], None] | None = None,
) -> IngestStats:
    """Insert messages in batches, committing per batch (interrupt-safe)."""
    stats = IngestStats()
    batch: list[tuple] = []

    def flush() -> None:
        if not batch:
            return
        # rowcount counts only the directly inserted rows (OR IGNORE skips are
        # excluded, and trigger-driven FTS writes don't inflate it).
        cur = conn.executemany(_INSERT_SQL, batch)
        conn.commit()
        stats.inserted += max(cur.rowcount, 0)
        batch.clear()
        if progress:
            progress(stats)

    for msg in messages:
        stats.seen += 1
        batch.append(_row(msg))
        if len(batch) >= BATCH_SIZE:
            flush()
    flush()
    return stats


def ingest_mbox(
    conn: sqlite3.Connection,
    mbox_path: Path | str,
    limit: int | None = None,
    progress: Callable[[IngestStats], None] | None = None,
) -> IngestStats:
    stats = insert_messages(conn, iter_mbox(mbox_path, limit=limit), progress=progress)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('mbox_source', ?)",
        (str(mbox_path),),
    )
    conn.commit()
    return stats
