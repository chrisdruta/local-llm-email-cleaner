"""Rule evaluation and persistence.

Semantics (all data-driven from rules.toml — the engine special-cases nothing
by name):

1. Every enabled rule is tested against every message; ALL matches are
   recorded in rule_hits (the policy gates refuse to auto-approve a message
   with any keep-voting hit, winner or not).
2. The winner is the highest-priority match, ties broken by file order
   (compile_ruleset already yields rules in that order, so the first match
   wins).
3. A winner with protect, or with confirm_with_llm = false, decides alone:
   staged_action is finalized with decision_source='rule'. A winner that wants
   LLM confirmation writes only its rule_* verdict, staged_action stays NULL.
4. No match at all -> only ruled_at is set; the LLM suggests the action.

"Awaiting LLM" is models.AWAITING_LLM_WHERE; human decisions made in the
review UI live on approved rows, which reset (pending-only) never touches.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter

from ..models import DecisionSource, finalize
from .matcher import CompiledRule, compile_ruleset
from .ruleset import RuleSet
from .views import MessageView, RuleContext

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

_UPDATE_SQL = """
UPDATE messages
SET ruled_at=datetime('now'), rule_name=?, rule_action=?, rule_category=?,
    rule_protected=?, rule_ephemeral=?, staged_action=?, decision_source=?
WHERE id=?
"""

_INSERT_HIT_SQL = (
    "INSERT INTO rule_hits (message_id, rule_name, action, won) VALUES (?, ?, ?, ?)"
)

#: decision columns owned by the rules stage, reset together
_RESET_RULE_COLS = """
ruled_at=NULL, rule_name=NULL, rule_action=NULL, rule_category=NULL,
rule_protected=0, rule_ephemeral=0, staged_action=NULL, decision_source=NULL
"""

_RESET_LLM_COLS = """
llm_action=NULL, llm_category=NULL, llm_confidence=NULL, llm_reason=NULL,
llm_ephemeral=0
"""


def evaluate_message(
    msg: MessageView, ctx: RuleContext, compiled: tuple[CompiledRule, ...]
) -> tuple[CompiledRule, ...]:
    """All rules matching this message, in evaluation (= winning) order."""
    return tuple(rule for rule in compiled if rule.matches(msg, ctx))


def run_rules(
    conn: sqlite3.Connection,
    ruleset: RuleSet,
    ctx: RuleContext,
    reset: bool = False,
    full: bool = False,
) -> Counter:
    """Evaluate all un-ruled messages; returns counts per outcome.

    Counter keys: the winner's action for rule-decided rows, "needs_llm" for
    rows handed to the classifier (rule pending confirmation or no match).

    reset re-evaluates every still-pending row (approved/applied rows keep
    their provenance) but PRESERVES stored LLM verdicts, re-finalizing from
    them afterwards — tuning rules.toml never re-pays LLM time. full=True
    wipes the LLM verdicts too.

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
        reset_cols = _RESET_RULE_COLS + (", " + _RESET_LLM_COLS if full else "")
        conn.execute(
            f"UPDATE messages SET {reset_cols} WHERE review_status = 'pending'"
        )
        conn.commit()

    compiled = compile_ruleset(ruleset)

    # Stream the corpus one BATCH_SIZE chunk at a time. Every evaluated row
    # gets a non-NULL ruled_at and is committed before the next fetch, so it
    # drops out of `ruled_at IS NULL` — memory stays bounded to one chunk even
    # though we pull body_text (which keep-keyword rules scan).
    counts: Counter = Counter()
    total = 0

    while True:
        rows = conn.execute(
            """
            SELECT id, from_addr, from_name, subject, labels, has_attachments,
                   list_unsubscribe, body_text
            FROM messages WHERE ruled_at IS NULL
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
            hits = evaluate_message(view, ctx, compiled)
            winner = hits[0].rule if hits else None

            if winner is None:
                counts["needs_llm"] += 1
                update_batch.append((None, None, None, 0, 0, None, None, view.id))
            else:
                decides_alone = winner.protect or not winner.confirm_with_llm
                if decides_alone:
                    counts[winner.action] += 1
                    action, source = winner.action, DecisionSource.RULE.value
                else:
                    counts["needs_llm"] += 1
                    action = source = None
                update_batch.append(
                    (
                        winner.name,
                        winner.action,
                        winner.category,
                        int(winner.protect),
                        int(winner.ephemeral),
                        action,
                        source,
                        view.id,
                    )
                )
            hit_batch.extend(
                (view.id, hit.name, hit.rule.action, int(hit is hits[0]))
                for hit in hits
            )

        conn.executemany(_UPDATE_SQL, update_batch)
        conn.executemany(_INSERT_HIT_SQL, hit_batch)
        conn.commit()
        total += len(rows)

    merged = finalize_with_stored_llm(conn)
    if merged:
        logger.info("Re-finalized %d rows from stored LLM verdicts", merged)

    logger.info("Rules evaluated %d messages: %s", total, dict(counts))
    return counts


def finalize_with_stored_llm(conn: sqlite3.Connection) -> int:
    """Finalize rows that already carry an LLM verdict (after a rules re-run).

    Uses the same agree/disagree logic as the classifier (models.finalize), so
    a rules.toml tuning pass only sends genuinely new rows back to the LLM.
    """
    rows = conn.execute(
        """
        SELECT id, rule_action, llm_action FROM messages
        WHERE staged_action IS NULL AND ruled_at IS NOT NULL
          AND llm_action IS NOT NULL
        """
    ).fetchall()
    if not rows:
        return 0
    updates = []
    decided = 0
    for row in rows:
        action, source = finalize(row["rule_action"], row["llm_action"])
        if action is None:
            continue  # still a disagreement — stays in the needs-decision queue
        decided += 1
        updates.append((action, source, row["id"]))
    if updates:
        conn.executemany(
            "UPDATE messages SET staged_action=?, decision_source=? WHERE id=?",
            updates,
        )
        conn.commit()
    return decided


def load_context(conn: sqlite3.Connection) -> RuleContext:
    contacts = frozenset(
        r["address"] for r in conn.execute("SELECT address FROM contacts")
    )
    return RuleContext(known_contacts=contacts)
