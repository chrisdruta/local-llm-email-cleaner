"""CSV export of the approved action table."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .review import queries

FIELDS = (
    "gmail_message_id",
    "rfc_message_id",
    "action",
    "reason",
    "confidence",
    "review_status",
    "from_addr",
    "subject",
    "date_utc",
)


def export_actions(conn: sqlite3.Connection, out_path: Path | str) -> int:
    rows = conn.execute(queries.EXPORT_ACTIONS).fetchall()
    with Path(out_path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        for row in rows:
            writer.writerow([row[field] for field in FIELDS])
    return len(rows)
