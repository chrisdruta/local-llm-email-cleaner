"""Lightweight message view + evaluation context the rules operate on."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MessageView:
    id: int
    from_addr: str | None
    from_name: str | None
    subject: str
    labels: frozenset[str]  # lowercased label names
    has_attachments: bool
    date_epoch: int | None
    list_unsubscribe: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> MessageView:
        raw_labels = row["labels"] or ""
        labels = frozenset(
            part.strip().lower() for part in raw_labels.split(",") if part.strip()
        )
        return cls(
            id=row["id"],
            from_addr=row["from_addr"],
            from_name=row["from_name"],
            subject=row["subject"] or "",
            labels=labels,
            has_attachments=bool(row["has_attachments"]),
            date_epoch=row["date_epoch"],
            list_unsubscribe=bool(row["list_unsubscribe"]),
        )


@dataclass(frozen=True)
class RuleContext:
    known_contacts: frozenset[str] = field(default_factory=frozenset)
    old_cutoff_epoch: int = 0  # messages with date_epoch < cutoff count as "old"

    def is_known_contact(self, addr: str | None) -> bool:
        return addr is not None and addr in self.known_contacts
