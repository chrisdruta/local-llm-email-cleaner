"""Batched, resumable LLM classification over the SQLite corpus.

Four populations get an LLM verdict:
- rule-ambiguous messages (staged NEEDS_REVIEW, untouched by rules);
- rule-staged DELETE_CANDIDATEs, which need an independent LLM confidence
  before the auto-trash policy gate may fire;
- rule-staged ARCHIVE_CANDIDATEs, for the same second-opinion confidence
  before auto-archive — the LLM may also escalate one to trash, or pull it
  back to human review if it disagrees; and
- keyword-protected KEEPs (a financial/security keyword rule fired). Those
  rules over-match promo footers, so the LLM re-checks them and may pull an
  obvious promo down to archive/trash/review. The protection rule_hit stays on
  the row, so a downgrade can never auto-approve — it only routes to a human.
  KEEPs from a known contact (the one absolute protection) are not in scope.

Requests are fanned out cfg.llm_concurrency at a time on our own thread
pool (chain.invoke per message — equivalent to LangChain's batch(), which
is also just thread-pooled invokes, but lets us cancel queued work on
Ctrl-C instead of draining it); all SQLite writes stay on the caller's
thread. Completion is recorded per row (ai_confidence becomes non-NULL)
and committed per message batch — including on interrupt — so an
interrupted run resumes for free.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter
from datetime import date
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from langchain_core.runnables import Runnable

from ..config import Config
from ..models import (
    CLASSIFIED_BY_LLM,
    CLASSIFIED_BY_RULES_LLM,
    LABEL_FOR_LLM_ACTION,
    PENDING_CLASSIFICATION_WHERE,
    ProposedAction,
    StagedLabel,
    pending_classification_params,
)
from .prompts import build_inputs
from .schema import EmailClassification

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

# Rule-ambiguous rows plus rule-staged delete/archive candidates needing a
# second opinion; voice-export delete candidates (decided by the export, backed
# up to disk) are excluded. The predicate is shared with `status` via models so
# the two can't drift — see models.PENDING_CLASSIFICATION_WHERE.
_SELECT_SQL = f"""
SELECT id, from_addr, from_name, subject, date_utc, labels, body_text, staged_label,
       ephemeral
FROM messages
WHERE {PENDING_CLASSIFICATION_WHERE}
ORDER BY id
"""


@dataclass
class ClassifyStats:
    processed: int = 0
    failed: int = 0
    by_action: Counter = field(default_factory=Counter)


def _normalize(result: object) -> EmailClassification | Exception:
    """Coerce one chain.batch() item to EmailClassification or an Exception."""
    if isinstance(result, Exception):
        return result
    if isinstance(result, EmailClassification):
        return result
    try:
        return EmailClassification.model_validate(result)
    except Exception as exc:
        return exc


def _batch_with_retry(
    chain: Runnable,
    inputs: list[dict],
    concurrency: int,
    on_done: Callable[[int, EmailClassification | Exception], None] | None = None,
) -> list[EmailClassification | Exception]:
    """Classify all inputs concurrently; retry only the failed ones.

    Returns one entry per input, in order. Items still failing after
    MAX_ATTEMPTS keep their last Exception. on_done fires once per input,
    on the caller's thread, when its result is final: successes as they
    stream in, failures only after retries are exhausted.

    On Ctrl-C (or any BaseException) queued requests are dropped and
    in-flight ones abandoned rather than drained — inputs that never got a
    final result simply aren't reported via on_done.
    """
    results: list[EmailClassification | Exception | None] = [None] * len(inputs)

    def run_pass(indexes: list[int]) -> None:
        executor = ThreadPoolExecutor(max_workers=concurrency)
        try:
            futures = {executor.submit(chain.invoke, inputs[i]): i for i in indexes}
            for future in as_completed(futures):
                i = futures[future]
                try:
                    r: object = future.result()
                except Exception as exc:
                    r = exc
                results[i] = _normalize(r)
                if on_done and not isinstance(results[i], Exception):
                    on_done(i, results[i])
        except BaseException:
            # Interrupted: don't start queued requests, don't wait for
            # in-flight ones (their worker threads are abandoned).
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        executor.shutdown()

    run_pass(list(range(len(inputs))))
    delay = 1.0
    for attempt in range(2, MAX_ATTEMPTS + 1):
        pending = [i for i, r in enumerate(results) if isinstance(r, Exception)]
        if not pending:
            break
        logger.warning(
            "%d of %d classifications failed; retrying (attempt %d)",
            len(pending),
            len(inputs),
            attempt,
        )
        time.sleep(delay)
        delay *= 2
        run_pass(pending)
    if on_done:
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                on_done(i, r)
    return results


# A rule-staged row gets the LLM as a SECOND OPINION; each staged label declares
# which LLM verdicts it accepts. An accepted verdict is applied as-is (mapped by
# LABEL_FOR_LLM_ACTION); any other verdict is a disagreement and the row falls
# back to human review. DELETE_CANDIDATE accepts only a confirming 'trash';
# ARCHIVE_CANDIDATE also lets the LLM escalate to 'trash' (handed to the stricter
# auto-trash gate). Labels absent here — KEEP (a keyword-protection second
# opinion that may pull an over-protected promo down) — accept every verdict,
# exactly like a primary NEEDS_REVIEW row: the LLM moves the message wherever it
# judges. A downgraded KEEP keeps its protection rule_hit, so the policy gate's
# not-protected check still routes it to a human rather than auto-approving.
_ACCEPTED_LLM_ACTIONS: dict[str, frozenset[str]] = {
    StagedLabel.DELETE_CANDIDATE.value: frozenset({ProposedAction.TRASH.value}),
    StagedLabel.ARCHIVE_CANDIDATE.value: frozenset(
        {ProposedAction.ARCHIVE.value, ProposedAction.TRASH.value}
    ),
}


def _updates_for(row: sqlite3.Row, result: EmailClassification) -> tuple:
    """Compute the column updates for one classified row."""
    staged_in = row["staged_label"]
    accepted = _ACCEPTED_LLM_ACTIONS.get(staged_in)  # None => accept all
    if accepted is not None and result.action not in accepted:
        # Disagreement with the rule's staging -> conservative: human review.
        staged = StagedLabel.NEEDS_REVIEW
        action = ProposedAction.REVIEW
    else:
        staged = LABEL_FOR_LLM_ACTION[result.action]
        action = ProposedAction(result.action)
    # NEEDS_REVIEW is the only population the rules left unjudged, so the LLM is
    # its sole classifier; every other staged label means rules + LLM both
    # weighed in (recorded as rules+llm), regardless of the verdict.
    classified_by = (
        CLASSIFIED_BY_LLM
        if staged_in == StagedLabel.NEEDS_REVIEW.value
        else CLASSIFIED_BY_RULES_LLM
    )
    # AND semantics: the age-floor waiver requires BOTH signals — the digest
    # rule must have set ephemeral during the rules stage (deterministic) AND
    # the LLM must confirm it. A non-digest row (ephemeral=0) can never become
    # ephemeral from the LLM alone, and a digest the LLM doesn't consider
    # ephemeral falls back to the normal 12-month floor.
    ephemeral = bool(row["ephemeral"]) and result.ephemeral
    return (
        staged.value,
        action.value,
        result.category,
        result.confidence,
        result.reason,
        classified_by,
        int(ephemeral),
        row["id"],
    )


_UPDATE_SQL = """
UPDATE messages
SET staged_label=?, proposed_action=?, ai_category=?, ai_confidence=?, ai_reason=?,
    classified_by=?, ephemeral=?
WHERE id=?
"""

_FAILURE_SQL = f"""
UPDATE messages
SET staged_label=?, proposed_action=?, ai_confidence=0.0, ai_reason=?,
    classified_by='{CLASSIFIED_BY_LLM}'
WHERE id=?
"""


def classify_messages(
    conn: sqlite3.Connection,
    cfg: Config,
    chain: Runnable,
    limit: int | None = None,
    progress: Callable[[ClassifyStats, int], None] | None = None,
) -> ClassifyStats:
    rows = conn.execute(_SELECT_SQL, pending_classification_params()).fetchall()
    if limit is not None:
        rows = rows[:limit]

    stats = ClassifyStats()

    def write_result(row: sqlite3.Row, result: EmailClassification | Exception) -> None:
        # Per-message write + progress: fires (on this thread) as each
        # in-flight request finalizes.
        if isinstance(result, Exception):
            # Confidence 0.0 marks the row done-but-untrusted: it lands in
            # human review and is excluded from re-selection and the gate.
            logger.error("Giving up on message %s: %s", row["id"], result)
            conn.execute(
                _FAILURE_SQL,
                (
                    StagedLabel.NEEDS_REVIEW.value,
                    ProposedAction.REVIEW.value,
                    f"classification failed: {result}",
                    row["id"],
                ),
            )
            stats.failed += 1
        else:
            conn.execute(_UPDATE_SQL, _updates_for(row, result))
            stats.processed += 1
            stats.by_action[result.action] += 1
        if progress:
            progress(stats, len(rows))

    today = date.today().isoformat()  # one clock read per run; given to the LLM
    for start in range(0, len(rows), cfg.llm_batch_size):
        chunk = rows[start : start + cfg.llm_batch_size]
        inputs = [build_inputs(row, cfg.max_body_chars, today) for row in chunk]
        try:
            _batch_with_retry(
                chain,
                inputs,
                cfg.llm_concurrency,
                lambda i, result: write_result(chunk[i], result),
            )
        except BaseException:
            # Ctrl-C mid-chunk: keep what finished; resume skips it next run.
            conn.commit()
            raise
        conn.commit()
    if progress:
        progress(stats, len(rows))
    return stats
