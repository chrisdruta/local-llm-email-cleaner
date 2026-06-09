"""Batched, resumable LLM classification over the SQLite corpus.

Three populations get an LLM verdict:
- rule-ambiguous messages (staged NEEDS_REVIEW, untouched by rules);
- rule-staged DELETE_CANDIDATEs, which need an independent LLM confidence
  before the auto-trash policy gate may fire; and
- rule-staged ARCHIVE_CANDIDATEs, for the same second-opinion confidence
  before auto-archive — the LLM may also escalate one to trash, or pull it
  back to human review if it disagrees.

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
    CLASSIFIED_BY_VOICE,
    LABEL_FOR_LLM_ACTION,
    ProposedAction,
    StagedLabel,
)
from .prompts import build_inputs
from .schema import EmailClassification

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

_SELECT_SQL = """
SELECT id, from_addr, from_name, subject, date_utc, labels, body_text, staged_label,
       ephemeral
FROM messages
WHERE review_status='pending' AND ai_confidence IS NULL
  AND (
        (staged_label=:needs_review AND classified_by IS NULL)
        -- Rule-staged delete/archive candidates get an independent LLM verdict
        -- (a second opinion before the auto-trash/auto-archive gates may fire).
        -- Voice-export delete candidates were decided by the export (backed up
        -- to disk) and must not be sent to the LLM; rule-staged ones still are.
     OR ((staged_label=:delete_candidate OR staged_label=:archive_candidate)
         AND classified_by IS NOT :voice)
  )
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


def _updates_for(row: sqlite3.Row, result: EmailClassification) -> tuple:
    """Compute the column updates for one classified row."""
    if row["staged_label"] == StagedLabel.DELETE_CANDIDATE.value:
        # Rules already staged this for deletion; the LLM is a second opinion.
        if result.action == ProposedAction.TRASH.value:
            staged = StagedLabel.DELETE_CANDIDATE
            action = ProposedAction.TRASH
        else:
            # Disagreement -> conservative: back to human review.
            staged = StagedLabel.NEEDS_REVIEW
            action = ProposedAction.REVIEW
        classified_by = CLASSIFIED_BY_RULES_LLM
    elif row["staged_label"] == StagedLabel.ARCHIVE_CANDIDATE.value:
        # Rules staged this for archive; the LLM is a second opinion that may
        # escalate to trash, agree (archive), or pull it back to review.
        if result.action == ProposedAction.TRASH.value:
            # Escalation: hand it to the stricter auto-trash gate, which still
            # independently enforces age / no-attachments / confidence >= 0.90.
            staged = StagedLabel.DELETE_CANDIDATE
            action = ProposedAction.TRASH
        elif result.action == ProposedAction.ARCHIVE.value:
            staged = StagedLabel.ARCHIVE_CANDIDATE
            action = ProposedAction.ARCHIVE
        else:
            # keep / review -> conservative: back to human review.
            staged = StagedLabel.NEEDS_REVIEW
            action = ProposedAction.REVIEW
        classified_by = CLASSIFIED_BY_RULES_LLM
    else:
        staged = LABEL_FOR_LLM_ACTION[result.action]
        action = ProposedAction(result.action)
        classified_by = CLASSIFIED_BY_LLM
    # OR semantics: never clear a deterministically-set ephemeral flag (the
    # digest rule may have set it during the rules stage); the LLM can only add.
    ephemeral = bool(row["ephemeral"]) or result.ephemeral
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
    rows = conn.execute(
        _SELECT_SQL,
        {
            "needs_review": StagedLabel.NEEDS_REVIEW.value,
            "delete_candidate": StagedLabel.DELETE_CANDIDATE.value,
            "archive_candidate": StagedLabel.ARCHIVE_CANDIDATE.value,
            "voice": CLASSIFIED_BY_VOICE,
        },
    ).fetchall()
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
