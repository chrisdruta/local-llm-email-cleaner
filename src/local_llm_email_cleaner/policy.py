"""The policy gates — the one place auto-approval can happen.

Auto-trash (every condition must hold):
    proposed_action == trash
    AND LLM confidence >= auto_trash_min_confidence (default 0.90)
    AND no attachments
    AND not from a known contact
    AND not protected (financial/legal/security rules)
    AND older than auto_trash_min_age_months
    AND matches at least one deterministic candidate rule

Auto-archive (laxer, because archiving is reversible and labeled in Gmail):
    proposed_action == archive
    AND matches at least one deterministic candidate rule
    AND not from a known contact
    AND not protected
    AND, when the LLM saw it, confidence >= auto_archive_min_confidence
        (rule-staged archive candidates skip the LLM, so NULL passes)

Everything else stays 'pending' for human review. Re-runnable after tuning
thresholds without re-running the LLM.
"""

from __future__ import annotations

import logging
import sqlite3

from .config import Config
from .ingest.headers import parse_epoch_to_age_cutoff

logger = logging.getLogger(__name__)

_GATE_SQL = """
UPDATE messages SET review_status='auto_approved', review_note='auto-trash policy gate'
WHERE review_status='pending'
  AND proposed_action='trash'
  AND classified_by LIKE '%llm%'
  AND ai_confidence IS NOT NULL AND ai_confidence >= :min_confidence
  AND has_attachments = 0
  AND date_epoch IS NOT NULL AND date_epoch < :age_cutoff
  AND (from_addr IS NULL OR from_addr NOT IN (SELECT address FROM contacts WHERE is_known=1))
  AND EXISTS (
        SELECT 1 FROM rule_hits
        WHERE rule_hits.message_id = messages.id AND rule_hits.rule_kind = 'candidate'
      )
  AND NOT EXISTS (
        SELECT 1 FROM rule_hits
        WHERE rule_hits.message_id = messages.id AND rule_hits.rule_kind = 'protection'
      )
"""

_ARCHIVE_GATE_SQL = """
UPDATE messages SET review_status='auto_approved', review_note='auto-archive policy gate'
WHERE review_status='pending'
  AND proposed_action='archive'
  -- Rule-staged archive candidates never see the LLM: NULL confidence counts
  -- as full confidence (and a threshold > 1 therefore disables the gate).
  AND COALESCE(ai_confidence, 1.0) >= :min_confidence
  AND (from_addr IS NULL OR from_addr NOT IN (SELECT address FROM contacts WHERE is_known=1))
  AND EXISTS (
        SELECT 1 FROM rule_hits
        WHERE rule_hits.message_id = messages.id AND rule_hits.rule_kind = 'candidate'
      )
  AND NOT EXISTS (
        SELECT 1 FROM rule_hits
        WHERE rule_hits.message_id = messages.id AND rule_hits.rule_kind = 'protection'
      )
"""


def apply_policy(conn: sqlite3.Connection, cfg: Config) -> dict[str, int]:
    """Run the gate; returns counts for reporting."""
    # Re-runnable: demote earlier auto-approvals first so threshold changes
    # take effect in both directions. Human decisions are never touched.
    conn.execute(
        "UPDATE messages SET review_status='pending', review_note=NULL "
        "WHERE review_status='auto_approved'"
    )

    cursor = conn.execute(
        _GATE_SQL,
        {
            "min_confidence": cfg.auto_trash_min_confidence,
            "age_cutoff": parse_epoch_to_age_cutoff(cfg.auto_trash_min_age_months),
        },
    )
    auto_trashed = cursor.rowcount
    cursor = conn.execute(
        _ARCHIVE_GATE_SQL, {"min_confidence": cfg.auto_archive_min_confidence}
    )
    auto_archived = cursor.rowcount
    conn.commit()

    pending_trash = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE review_status='pending' AND proposed_action='trash'"
    ).fetchone()[0]
    pending_archive = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE review_status='pending' AND proposed_action='archive'"
    ).fetchone()[0]
    logger.info(
        "Policy gates: %d auto-approved for trash (%d left for review), "
        "%d for archive (%d left for review)",
        auto_trashed,
        pending_trash,
        auto_archived,
        pending_archive,
    )
    return {
        "auto_approved": auto_trashed,
        "auto_archived": auto_archived,
        "pending_trash_for_review": pending_trash,
        "pending_archive_for_review": pending_archive,
    }
