"""Lightweight message view + evaluation context the rules operate on."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..models import split_labels


@dataclass(frozen=True)
class MessageView:
    id: int
    from_addr: str | None
    from_name: str | None
    subject: str
    labels: frozenset[str]  # lowercased label names
    has_attachments: bool
    list_unsubscribe: bool
    #: extracted plain-text body (capped at ingest); protection rules scan it
    #: so sensitive mail with an innocuous subject is still caught.
    body_text: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> MessageView:
        labels = frozenset(split_labels(row["labels"]))
        return cls(
            id=row["id"],
            from_addr=row["from_addr"],
            from_name=row["from_name"],
            subject=row["subject"] or "",
            labels=labels,
            has_attachments=bool(row["has_attachments"]),
            list_unsubscribe=bool(row["list_unsubscribe"]),
            body_text=row["body_text"] or "",
        )


@dataclass(frozen=True)
class RuleContext:
    known_contacts: frozenset[str] = field(default_factory=frozenset)

    def is_known_contact(self, addr: str | None) -> bool:
        return addr is not None and addr in self.known_contacts
