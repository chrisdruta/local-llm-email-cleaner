"""Rule evaluation and persistence.

Semantics:
0. Synthetic records (a candidate vote carrying skip_llm — Google Voice
   SMS/call/voicemail) are decided before everything else. They are converter
   artifacts addressed `<number>@unknown.email`, not real correspondence, and
   the outbound-SMS copy can leak that number into the derived contacts — so
   the known-contact protection would otherwise force KEEP and shield them from
   cleanup. skip_llm is the only signal used here; no rule is matched by name.
1. Protection rules run next — a hit forces KEEP and excludes the message
   from LLM classification and the policy gates. Exception: Gmail's own Spam
   label overrides the keyword protections (scam bait imitates exactly those
   subjects) but never known_contact; overridden hits are still recorded, so
   the gates refuse to auto-approve such messages — human review only.
2. Candidate rules vote cleanup classes; the winning vote is the one with the
   highest RuleVote.priority, ties broken by the most conservative staged_label
   (ARCHIVE > DELETE > UNSUBSCRIBE). Every hit is recorded in rule_hits. The
   winner's own fields (ephemeral, skip_llm) drive the disposition — the engine
   does not special-case any rule by name.
3. No hits at all -> NEEDS_REVIEW, left for the LLM classifier.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass

from ..models import (
    ACTION_FOR_LABEL,
    CLASSIFIED_BY_RULES,
    CLASSIFIED_BY_VOICE,
    RuleVote,
    StagedLabel,
)
from . import patterns
from .candidate_rules import CANDIDATE_RULES
from .protection_rules import ABSOLUTE_PROTECTION_RULES, OVERRIDABLE_PROTECTION_RULES
from .views import MessageView, RuleContext

logger = logging.getLogger(__name__)

# Most conservative outcome first; breaks ties among equal-priority votes.
_CANDIDATE_PRECEDENCE = (
    StagedLabel.ARCHIVE_CANDIDATE,
    StagedLabel.DELETE_CANDIDATE,
    StagedLabel.UNSUBSCRIBE_CANDIDATE,
)


def _select_candidate(votes: tuple[RuleVote, ...]) -> RuleVote:
    """Pick the winning candidate vote: highest priority, then most conservative
    staged_label. The choice is data-driven — no rule is matched by name."""
    top = max(v.priority for v in votes)
    contenders = [v for v in votes if v.priority == top]
    if len(contenders) == 1:
        return contenders[0]
    by_label = {v.staged_label: v for v in reversed(contenders)}  # keep first seen
    for label in _CANDIDATE_PRECEDENCE:
        if label in by_label:
            return by_label[label]
    return contenders[0]


@dataclass(frozen=True)
class RuleResult:
    staged_label: StagedLabel
    category: str | None
    hits: tuple[RuleVote, ...]
    ephemeral: bool = False
    #: overrides the engine's default classified_by ('rules') for this row —
    #: voice messages use 'voice' so the LLM classifier skips them.
    classified_by: str | None = None


def evaluate_message(msg: MessageView, ctx: RuleContext) -> RuleResult:
    candidate_hits = tuple(v for rule in CANDIDATE_RULES if (v := rule(msg, ctx)))

    # Synthetic records (skip_llm — Google Voice) are decided before protection:
    # they are converter artifacts, not correspondence, and a leaked phone number
    # in the contacts must not let known_contact shield them from cleanup. They
    # are tagged CLASSIFIED_BY_VOICE so the classifier skips them and the auto-
    # trash gate never fires (skip_llm => ai_confidence stays NULL => review).
    synthetic_hits = tuple(v for v in candidate_hits if v.skip_llm)
    if synthetic_hits:
        winner = _select_candidate(synthetic_hits)
        return RuleResult(
            winner.staged_label,
            winner.category,
            candidate_hits,
            ephemeral=winner.ephemeral,
            classified_by=CLASSIFIED_BY_VOICE,
        )

    absolute_hits = tuple(
        v for rule in ABSOLUTE_PROTECTION_RULES if (v := rule(msg, ctx))
    )
    keyword_hits = tuple(
        v for rule in OVERRIDABLE_PROTECTION_RULES if (v := rule(msg, ctx))
    )
    spam = bool(msg.labels & patterns.SPAM_LABELS)
    if absolute_hits or (keyword_hits and not spam):
        hits = absolute_hits + keyword_hits
        return RuleResult(StagedLabel.KEEP, hits[0].category, hits)

    # Spam override: keyword protection hits are still recorded below, so the
    # policy gates can never auto-approve these — they always reach a human.
    #
    # The winning vote's own fields drive the disposition — no rule is matched by
    # name. An ephemeral vote (digest) lets the gate waive the age floor once the
    # LLM also confirms; priority lets a vote beat the ordinary ARCHIVE > DELETE >
    # UNSUBSCRIBE precedence. (skip_llm votes were already handled above.)
    if candidate_hits:
        winner = _select_candidate(candidate_hits)
        return RuleResult(
            winner.staged_label,
            winner.category,
            keyword_hits + candidate_hits,
            ephemeral=winner.ephemeral,
        )

    return RuleResult(StagedLabel.NEEDS_REVIEW, None, keyword_hits + candidate_hits)


BATCH_SIZE = 500

_UPDATE_SQL = """
UPDATE messages
SET staged_label=?, proposed_action=?, ai_category=?, classified_by=?, ephemeral=?
WHERE id=?
"""

_INSERT_HIT_SQL = "INSERT INTO rule_hits (message_id, rule_name, rule_kind, outcome) VALUES (?, ?, ?, ?)"


def run_rules(
    conn: sqlite3.Connection, ctx: RuleContext, reset: bool = False
) -> Counter:
    """Evaluate all un-ruled messages; returns counts per staged label.

    Writes are flushed per BATCH_SIZE chunk (interrupt-safe, like ingest).
    """
    if reset:
        # Only pending rows are re-evaluated below, so only their hits may be
        # deleted — wiping all rule_hits would permanently orphan approved/
        # applied rows from the record of why they were staged.
        conn.execute(
            """
            DELETE FROM rule_hits WHERE message_id IN
                (SELECT id FROM messages WHERE review_status = 'pending')
            """
        )
        conn.execute(
            """
            UPDATE messages SET staged_label=NULL, proposed_action=NULL, ai_category=NULL,
                   ai_confidence=NULL, ai_reason=NULL, classified_by=NULL, ephemeral=0
            WHERE review_status = 'pending'
            """
        )
        conn.commit()

    # Stream the corpus one BATCH_SIZE chunk at a time. Every evaluated row gets
    # a non-NULL staged_label and is committed before the next fetch, so it
    # drops out of `staged_label IS NULL` — memory stays bounded to one chunk
    # even though we now pull body_text (which protection rules scan).
    counts: Counter = Counter()
    total = 0

    while True:
        rows = conn.execute(
            """
            SELECT id, from_addr, from_name, subject, labels, has_attachments,
                   list_unsubscribe, body_text
            FROM messages WHERE staged_label IS NULL
            ORDER BY id LIMIT ?
            """,
            (BATCH_SIZE,),
        ).fetchall()
        if not rows:
            break

        update_batch: list[tuple] = []
        hit_batch: list[tuple] = []
        for row in rows:
            view = MessageView.from_row(row)
            result = evaluate_message(view, ctx)
            counts[result.staged_label.value] += 1

            classified_by = result.classified_by or (
                CLASSIFIED_BY_RULES
                if result.staged_label != StagedLabel.NEEDS_REVIEW
                else None
            )
            update_batch.append(
                (
                    result.staged_label.value,
                    ACTION_FOR_LABEL[result.staged_label].value,
                    result.category,
                    classified_by,
                    int(result.ephemeral),
                    view.id,
                )
            )
            hit_batch.extend(
                (view.id, hit.rule_name, hit.rule_kind.value, hit.staged_label.value)
                for hit in result.hits
            )

        conn.executemany(_UPDATE_SQL, update_batch)
        conn.executemany(_INSERT_HIT_SQL, hit_batch)
        conn.commit()
        total += len(rows)

    logger.info("Rules evaluated %d messages: %s", total, dict(counts))
    return counts


def load_context(conn: sqlite3.Connection) -> RuleContext:
    contacts = frozenset(
        r["address"] for r in conn.execute("SELECT address FROM contacts")
    )
    return RuleContext(known_contacts=contacts)
