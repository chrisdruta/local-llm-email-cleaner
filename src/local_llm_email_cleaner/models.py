"""Core enums and dataclasses shared across pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class StagedLabel(StrEnum):
    KEEP = "KEEP"
    DELETE_CANDIDATE = "DELETE_CANDIDATE"
    ARCHIVE_CANDIDATE = "ARCHIVE_CANDIDATE"
    UNSUBSCRIBE_CANDIDATE = "UNSUBSCRIBE_CANDIDATE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ProposedAction(StrEnum):
    KEEP = "keep"
    ARCHIVE = "archive"
    TRASH = "trash"
    REVIEW = "review"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    AUTO_APPROVED = "auto_approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    SKIPPED = "skipped"


class RuleKind(StrEnum):
    PROTECTION = "protection"
    CANDIDATE = "candidate"


#: staged label -> default proposed action
ACTION_FOR_LABEL: dict[StagedLabel, ProposedAction] = {
    StagedLabel.KEEP: ProposedAction.KEEP,
    StagedLabel.DELETE_CANDIDATE: ProposedAction.TRASH,
    StagedLabel.ARCHIVE_CANDIDATE: ProposedAction.ARCHIVE,
    StagedLabel.UNSUBSCRIBE_CANDIDATE: ProposedAction.REVIEW,
    StagedLabel.NEEDS_REVIEW: ProposedAction.REVIEW,
}

#: LLM action string -> staged label
LABEL_FOR_LLM_ACTION: dict[str, StagedLabel] = {
    "keep": StagedLabel.KEEP,
    "archive": StagedLabel.ARCHIVE_CANDIDATE,
    "trash": StagedLabel.DELETE_CANDIDATE,
    "review": StagedLabel.NEEDS_REVIEW,
}


@dataclass(frozen=True)
class ParsedMessage:
    """One message extracted from the MBOX, ready for insertion."""

    gmail_msgid: str | None
    thread_id: str | None
    rfc_message_id: str | None
    labels: str | None  # raw comma-joined X-Gmail-Labels
    date_utc: str | None  # ISO-8601 UTC
    date_epoch: int | None
    from_addr: str | None
    from_name: str | None
    from_domain: str | None
    to_addr: str | None
    to_all: str | None  # comma-joined normalized To/Cc addresses
    subject: str | None
    snippet: str | None
    body_text: str | None
    has_attachments: bool
    attachment_names: list[str] = field(default_factory=list)
    size_bytes: int | None = None
    list_unsubscribe: bool = False

    def label_set(self) -> set[str]:
        if not self.labels:
            return set()
        return {part.strip().lower() for part in self.labels.split(",") if part.strip()}


@dataclass(frozen=True)
class RuleVote:
    """The outcome a single rule votes for on a message."""

    rule_name: str
    rule_kind: RuleKind
    staged_label: StagedLabel
    category: str


@dataclass(frozen=True)
class Classification:
    """A structured classification result (from rules or the LLM)."""

    action: ProposedAction
    category: str
    confidence: float
    reason: str
