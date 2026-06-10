"""Core enums and dataclasses shared across pipeline stages.

The v3 decision lifecycle, one writer per column:

  ingest   -> identity/content columns; review_status defaults to 'pending'
  rules    -> ruled_at + rule_* (and, when the winning rule decides alone,
              action + decision_source='rule'); every match goes to rule_hits
  classify -> llm_* + action + decision_source ('llm' when no rule matched,
              'rule+llm' when confirming a rule; disagreement -> 'review')
  policy   -> review_status pending <-> auto_approved (the only auto-approval)
  review   -> review_status approved / rejected (human)
  runner   -> review_status applied / skipped (after Gmail reconcile+mutate)

"Awaiting LLM classification" is simply `action IS NULL AND ruled_at IS NOT
NULL` — no shared predicate machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Action(StrEnum):
    """The single action vocabulary, shared by rules, the LLM, and the runner.

    REVIEW means "a human must decide" — rules never stage it (a rule that
    can't decide shouldn't match), but the LLM may return it and a
    rule-vs-LLM disagreement resolves to it.
    """

    KEEP = "keep"
    ARCHIVE = "archive"
    TRASH = "trash"
    REVIEW = "review"


class DecisionSource(StrEnum):
    """Who produced messages.action."""

    RULE = "rule"  # a rule decided alone (protect, or confirm_with_llm=false)
    LLM = "llm"  # no rule matched; the LLM's suggestion stands
    RULE_LLM = "rule+llm"  # rule staged it, LLM weighed in (confirm or dispute)


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    AUTO_APPROVED = "auto_approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    SKIPPED = "skipped"


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
    Action.TRASH.value,
    Action.ARCHIVE.value,
)

#: review statuses the runner (and the export preview) treat as approved
APPROVABLE_STATUSES: tuple[str, ...] = (
    ReviewStatus.APPROVED.value,
    ReviewStatus.AUTO_APPROVED.value,
)


def finalize(rule_action: str | None, llm_action: str) -> tuple[str, str]:
    """Resolve a message's final (action, decision_source) once the LLM has
    spoken. The one place rule-vs-LLM agreement is decided:

    - no rule matched          -> the LLM's suggestion stands
    - LLM agrees with the rule -> confirmed
    - disagreement             -> a human decides (strict equality; the LLM is
                                  deliberately blind to the rule's verdict, so
                                  agreement is meaningful)
    """
    if rule_action is None:
        return llm_action, DecisionSource.LLM.value
    if rule_action == llm_action:
        return rule_action, DecisionSource.RULE_LLM.value
    return Action.REVIEW.value, DecisionSource.RULE_LLM.value


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


#: Canonical llm_category slugs. The LLM is constrained to these (Literal in
#: llm/schema.py) so the column stays a small, groupable vocabulary instead of
#: free text. Rule categories (rules.toml) are free strings and usually a
#: subset. "digest" marks timely/disposable roundups (see the ephemeral flags).
CATEGORIES: tuple[str, ...] = (
    "promotion",
    "newsletter",
    "social",
    "digest",
    "notification",
    "shipping",
    "receipt",
    "calendar",
    "automated",
    "spam",
    "personal",
    "security",
    "financial_legal_medical",
    "other",
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
