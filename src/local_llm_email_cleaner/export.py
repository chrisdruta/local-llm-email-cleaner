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

#: free-text columns sourced from email content — formula-injection vectors
_TEXT_FIELDS = {"reason", "from_addr", "subject"}

#: leading characters Excel/Sheets interpret as a formula (CWE-1236)
_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_cell(value: object) -> object:
    """Neutralize spreadsheet formula injection in attacker-controlled text."""
    if isinstance(value, str) and value.startswith(_FORMULA_CHARS):
        return "'" + value
    return value


def export_actions(conn: sqlite3.Connection, out_path: Path | str) -> int:
    rows = conn.execute(queries.EXPORT_ACTIONS).fetchall()
    with Path(out_path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        for row in rows:
            writer.writerow(
                [
                    _sanitize_cell(row[field]) if field in _TEXT_FIELDS else row[field]
                    for field in FIELDS
                ]
            )
    return len(rows)
