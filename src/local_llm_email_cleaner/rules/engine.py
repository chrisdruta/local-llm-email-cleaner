"""Rule evaluation and persistence.

Semantics:
1. Protection rules run first — a hit forces KEEP and excludes the message
   from LLM classification and the policy gates. Exception: Gmail's own Spam
   label overrides the keyword protections (scam bait imitates exactly those
   subjects) but never known_contact; overridden hits are still recorded, so
   the gates refuse to auto-approve such messages — human review only.
2. Candidate rules vote cleanup classes; the most conservative wins
   (ARCHIVE > DELETE > UNSUBSCRIBE), but every hit is recorded in rule_hits.
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

# Most conservative outcome first; trumps later entries when several rules fire.
_CANDIDATE_PRECEDENCE = (
    StagedLabel.ARCHIVE_CANDIDATE,
    StagedLabel.DELETE_CANDIDATE,
    StagedLabel.UNSUBSCRIBE_CANDIDATE,
)


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
    candidate_hits = tuple(v for rule in CANDIDATE_RULES if (v := rule(msg, ctx)))

    # Voice override: Google Voice SMS / call-log / voicemail records are
    # synthetic emails, not real mail — they're backed up to disk by
    # `voice-export` and always staged for trash. Tagged 'voice' so the LLM
    # classifier skips them (it can't meaningfully judge a text message), which
    # also keeps them out of the auto-trash gate (human review only).
    voice_hit = next((v for v in candidate_hits if v.rule_name == "voice"), None)
    if voice_hit is not None:
        return RuleResult(
            StagedLabel.DELETE_CANDIDATE,
            voice_hit.category,
            keyword_hits + candidate_hits,
            classified_by=CLASSIFIED_BY_VOICE,
        )

    # Digest override: a timely/recurring digest is known-disposable, so it
    # beats the normal "most conservative wins" precedence (a Reddit digest
    # also hits updates_label -> ARCHIVE) and is marked ephemeral, letting the
    # policy gate auto-trash it without the usual age floor. The LLM still gets
    # the final say in `classify` (DELETE_CANDIDATEs are second-opinioned).
    digest_hit = next((v for v in candidate_hits if v.rule_name == "digest"), None)
    if digest_hit is not None:
        return RuleResult(
            StagedLabel.DELETE_CANDIDATE,
            digest_hit.category,
            keyword_hits + candidate_hits,
            ephemeral=True,
        )

    if candidate_hits:
        for label in _CANDIDATE_PRECEDENCE:
            winners = [v for v in candidate_hits if v.staged_label == label]
            if winners:
                return RuleResult(
                    label, winners[0].category, keyword_hits + candidate_hits
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
