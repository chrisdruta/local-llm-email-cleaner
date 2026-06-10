"""The policy gates — the one place auto-approval can happen.

Auto-trash (every condition must hold):
    action == trash, staged by a deterministic rule (rule_action == trash)
    AND LLM confidence >= auto_trash_min_confidence
        (or, when auto_trash_allow_rule_only is enabled, the rule decided
         alone — the one way rule-only trash can auto-approve;
         or no rule matched but the LLM cleared the higher
         auto_llm_only_min_confidence bar)
    AND no attachments
    AND not from a known contact
    AND no keep-voting rule hit and not protect-won
    AND older than auto_trash_min_age_months
        (waived when BOTH the rule and the LLM flag the message ephemeral —
         digests are worthless once their day passes — which only needs
         auto_trash_ephemeral_min_age_days of grace)

Auto-archive (laxer, because archiving is reversible and labeled in Gmail):
    action == archive, staged by a deterministic rule (rule_action == archive)
    AND not from a known contact
    AND no keep-voting rule hit and not protect-won
    AND, when the LLM saw it, confidence >= auto_archive_min_confidence
        (a NULL confidence — rule decided alone — counts as full confidence,
         so a threshold > 1 disables this gate)

Everything else stays 'pending' for human review. Re-runnable after tuning
without re-running the LLM, and preview_policy() shows exactly what
apply_policy() would approve — both are built from the same predicates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass

from .config import Config
from .ingest.headers import parse_epoch_to_age_cutoff, parse_epoch_to_age_cutoff_days
from .models import Action, DecisionSource

logger = logging.getLogger(__name__)

#: meta-table key where UI-tuned params persist (precedence: meta > config)
POLICY_PARAMS_META_KEY = "policy_params"


@dataclass(frozen=True)
class PolicyParams:
    auto_trash_min_confidence: float
    auto_trash_min_age_months: int
    auto_trash_ephemeral_min_age_days: int
    auto_archive_min_confidence: float
    #: let trash rules with confirm_with_llm=false auto-approve without an LLM
    #: confidence. Off by default: voice rows and other rule-only trash then
    #: always require explicit human approval.
    auto_trash_allow_rule_only: bool = False
    #: let a no-rule-matched message auto-approve on the LLM's word alone when
    #: its confidence clears this (deliberately higher) bar. All structural
    #: guards still apply. Set > 1 to disable the llm-only path.
    auto_llm_only_min_confidence: float = 0.95

    @classmethod
    def from_config(cls, cfg: Config) -> PolicyParams:
        return cls(
            auto_trash_min_confidence=cfg.auto_trash_min_confidence,
            auto_trash_min_age_months=cfg.auto_trash_min_age_months,
            auto_trash_ephemeral_min_age_days=cfg.auto_trash_ephemeral_min_age_days,
            auto_archive_min_confidence=cfg.auto_archive_min_confidence,
            auto_trash_allow_rule_only=cfg.auto_trash_allow_rule_only,
            auto_llm_only_min_confidence=cfg.auto_llm_only_min_confidence,
        )

    @classmethod
    def load(cls, conn: sqlite3.Connection, cfg: Config) -> PolicyParams:
        """Effective params: UI-tuned values saved in `meta` win over config."""
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (POLICY_PARAMS_META_KEY,)
        ).fetchone()
        base = cls.from_config(cfg)
        if row is None:
            return base
        try:
            saved = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            logger.warning("Ignoring unparseable %s in meta", POLICY_PARAMS_META_KEY)
            return base
        known = {k: v for k, v in saved.items() if k in asdict(base)}
        return cls(**{**asdict(base), **known})

    def save(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (POLICY_PARAMS_META_KEY, json.dumps(asdict(self))),
        )
        conn.commit()


# Safety predicates shared by BOTH gates. Edit here, never in one gate only —
# the gates must never drift apart on who is excluded from auto-approval.
_NOT_KNOWN_CONTACT = """
  (from_addr IS NULL OR from_addr NOT IN (SELECT address FROM contacts))
"""

# Any keep-voting rule hit blocks auto-approval, winner or not: a protect rule
# outranked by voice, or a keep keyword outranked by the spam label, still
# means "a human looks at this". rule_protected additionally covers the
# protect-won case directly.
_NO_KEEP_HIT = f"""
  rule_protected = 0
  AND NOT EXISTS (
        SELECT 1 FROM rule_hits
        WHERE rule_hits.message_id = messages.id
          AND rule_hits.action = '{Action.KEEP.value}'
      )
"""


def _trash_gate_where() -> str:
    return f"""
  review_status='pending'
  AND action='{Action.TRASH.value}'
  AND (
        -- staged by a deterministic trash rule AND confirmed by the LLM
        (rule_action='{Action.TRASH.value}'
         AND decision_source='{DecisionSource.RULE_LLM.value}'
         AND llm_confidence IS NOT NULL AND llm_confidence >= :min_confidence)
        -- a trash rule that decided alone (opt-in)
        OR (rule_action='{Action.TRASH.value}'
            AND :allow_rule_only AND decision_source='{DecisionSource.RULE.value}')
        -- no rule matched, but the LLM is exceptionally confident (the higher
        -- llm-only bar; > 1 disables this path)
        OR (decision_source='{DecisionSource.LLM.value}'
            AND llm_confidence IS NOT NULL
            AND llm_confidence >= :llm_only_min_confidence)
      )
  AND has_attachments = 0
  -- Age floor, waived only when BOTH the rule and the LLM flagged the message
  -- ephemeral (digests — worthless once their day passes); the short grace
  -- still applies. Everything else needs the full auto_trash_min_age_months.
  -- (LLM-only rows always need the full floor: rule_ephemeral is 0 for them.)
  AND date_epoch IS NOT NULL AND (
        date_epoch < :age_cutoff
        OR (rule_ephemeral = 1 AND llm_ephemeral = 1
            AND date_epoch < :ephemeral_age_cutoff)
      )
  AND {_NOT_KNOWN_CONTACT}
  AND {_NO_KEEP_HIT}
"""


def _archive_gate_where() -> str:
    return f"""
  review_status='pending'
  AND action='{Action.ARCHIVE.value}'
  AND (
        -- staged by a deterministic archive rule. A NULL confidence (rule
        -- decided alone) counts as full confidence, so a threshold > 1
        -- disables this path entirely.
        (rule_action='{Action.ARCHIVE.value}'
         AND COALESCE(llm_confidence, 1.0) >= :min_confidence)
        -- no rule matched, but the LLM is exceptionally confident
        OR (decision_source='{DecisionSource.LLM.value}'
            AND llm_confidence IS NOT NULL
            AND llm_confidence >= :llm_only_min_confidence)
      )
  AND {_NOT_KNOWN_CONTACT}
  AND {_NO_KEEP_HIT}
"""


def _trash_params(params: PolicyParams) -> dict:
    return {
        "min_confidence": params.auto_trash_min_confidence,
        "allow_rule_only": int(params.auto_trash_allow_rule_only),
        "llm_only_min_confidence": params.auto_llm_only_min_confidence,
        "age_cutoff": parse_epoch_to_age_cutoff(params.auto_trash_min_age_months),
        "ephemeral_age_cutoff": parse_epoch_to_age_cutoff_days(
            params.auto_trash_ephemeral_min_age_days
        ),
    }


def _archive_params(params: PolicyParams) -> dict:
    return {
        "min_confidence": params.auto_archive_min_confidence,
        "llm_only_min_confidence": params.auto_llm_only_min_confidence,
    }


@dataclass(frozen=True)
class GatePreview:
    """What apply_policy() would auto-approve right now."""

    trash_count: int
    archive_count: int
    trash_sample: list[sqlite3.Row]
    archive_sample: list[sqlite3.Row]


_SAMPLE_COLS = "id, date_utc, from_addr, subject, rule_name, llm_confidence, llm_reason"


def preview_policy(
    conn: sqlite3.Connection, params: PolicyParams, sample_limit: int = 50
) -> GatePreview:
    """Dry-run the gates: counts + sample rows, no writes.

    Built from the same WHERE clauses as apply_policy, so the preview cannot
    drift from what execution would do. NOTE: counts assume a fresh gate run —
    rows currently auto_approved are first demoted by apply_policy, so the
    preview counts them too via the review_status IN check below.
    """

    def gather(where: str, bind: dict) -> tuple[int, list[sqlite3.Row]]:
        # Preview must see rows apply_policy would re-gate after demoting
        # earlier auto-approvals, hence pending OR auto_approved.
        where = where.replace(
            "review_status='pending'",
            "review_status IN ('pending', 'auto_approved')",
            1,
        )
        count = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {where}", bind
        ).fetchone()[0]
        sample = conn.execute(
            f"SELECT {_SAMPLE_COLS} FROM messages WHERE {where} "
            f"ORDER BY date_epoch LIMIT {int(sample_limit)}",
            bind,
        ).fetchall()
        return count, sample

    trash_count, trash_sample = gather(_trash_gate_where(), _trash_params(params))
    archive_count, archive_sample = gather(
        _archive_gate_where(), _archive_params(params)
    )
    return GatePreview(trash_count, archive_count, trash_sample, archive_sample)


def apply_policy(conn: sqlite3.Connection, params: PolicyParams) -> dict[str, int]:
    """Run the gates; returns counts for reporting."""
    # Re-runnable: demote earlier auto-approvals first so threshold changes
    # take effect in both directions. Human decisions are never touched.
    conn.execute(
        "UPDATE messages SET review_status='pending', review_note=NULL "
        "WHERE review_status='auto_approved'"
    )

    cursor = conn.execute(
        f"UPDATE messages SET review_status='auto_approved', "
        f"review_note='auto-trash policy gate' WHERE {_trash_gate_where()}",
        _trash_params(params),
    )
    auto_trashed = cursor.rowcount
    cursor = conn.execute(
        f"UPDATE messages SET review_status='auto_approved', "
        f"review_note='auto-archive policy gate' WHERE {_archive_gate_where()}",
        _archive_params(params),
    )
    auto_archived = cursor.rowcount
    conn.commit()

    pending_trash = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE review_status='pending' AND action='trash'"
    ).fetchone()[0]
    pending_archive = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE review_status='pending' AND action='archive'"
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
