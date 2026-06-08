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


class ActionStatus(StrEnum):
    """Lifecycle of one `actions` audit row.

    ATTEMPT is written (and committed) before a live Gmail mutation; it is
    finalized to SUCCESS or ERROR afterwards. A lingering ATTEMPT row with a
    NULL completed_at means the process died mid-mutation. `actions.dry_run`
    is an orthogonal boolean, not a status.
    """

    ATTEMPT = "attempt"
    SUCCESS = "success"
    ERROR = "error"
    SKIPPED = "skipped"


#: actions the runner will execute (everything else is review-only)
ACTIONABLE_ACTIONS: tuple[str, ...] = (
    ProposedAction.TRASH.value,
    ProposedAction.ARCHIVE.value,
)

#: review statuses the runner (and the export preview) treat as approved
APPROVABLE_STATUSES: tuple[str, ...] = (
    ReviewStatus.APPROVED.value,
    ReviewStatus.AUTO_APPROVED.value,
)

#: messages.classified_by values — written by the rules engine / classifier
CLASSIFIED_BY_RULES = "rules"
CLASSIFIED_BY_LLM = "llm"
CLASSIFIED_BY_RULES_LLM = "rules+llm"
#: messages dispositioned by the Google Voice export (backed up to disk, then
#: staged for trash). Distinct so the LLM classifier skips them even though
#: they are DELETE_CANDIDATEs — they were decided by the export, not the rules.
CLASSIFIED_BY_VOICE = "voice"

#: classified_by values that carry an LLM verdict (both must match wherever
#: "seen by the LLM" is filtered — equality to 'llm' alone would be a bug)
LLM_CLASSIFIERS: tuple[str, ...] = (CLASSIFIED_BY_LLM, CLASSIFIED_BY_RULES_LLM)


def sql_in_list(values: tuple[str, ...]) -> str:
    """Render code-defined enum values for a SQL ``IN (...)`` clause.

    Only ever call this with the module-level constants above (trusted,
    enum-derived strings) — never with user input.
    """
    return ", ".join(f"'{v}'" for v in values)


def split_labels(raw: str | None) -> set[str]:
    """Split a raw comma-joined X-Gmail-Labels string into lowercased names."""
    if not raw:
        return set()
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


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

# The LLM action vocabulary is declared three times (the Literal in
# llm/schema.py, the dict keys above, and ProposedAction); fail loudly at
# import if they ever drift.
assert set(LABEL_FOR_LLM_ACTION) == {a.value for a in ProposedAction}, (
    "LABEL_FOR_LLM_ACTION keys must match ProposedAction values"
)


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
    body_text: str | None
    has_attachments: bool
    attachment_names: list[str] = field(default_factory=list)
    size_bytes: int | None = None
    list_unsubscribe: bool = False


@dataclass(frozen=True)
class RuleVote:
    """The outcome a single rule votes for on a message."""

    rule_name: str
    rule_kind: RuleKind
    staged_label: StagedLabel
    category: str
