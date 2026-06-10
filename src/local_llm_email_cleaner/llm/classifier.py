"""Batched, resumable LLM classification over the SQLite corpus.

The population is one line: every ruled message without a final action —
i.e. rows no rule matched (the LLM suggests the action) plus rows whose
winning rule asked for confirmation (confirm_with_llm in rules.toml). The
LLM is deliberately blind to the rule's tentative verdict; agreement
confirms it, disagreement routes the message to human review
(models.finalize is the one place that resolution lives).

Requests are fanned out cfg.llm_concurrency at a time on our own thread
pool (chain.invoke per message — equivalent to LangChain's batch(), which
is also just thread-pooled invokes, but lets us cancel queued work on
Ctrl-C instead of draining it); all SQLite writes stay on the caller's
thread. Completion is recorded per row (action becomes non-NULL) and
committed per message batch — including on interrupt — so an interrupted
run resumes for free.
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
from ..models import Action, DecisionSource, finalize
from .prompts import build_inputs
from .schema import EmailClassification

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

_SELECT_SQL = """
SELECT id, from_addr, from_name, subject, date_utc, labels, body_text, rule_action
FROM messages
WHERE action IS NULL AND ruled_at IS NOT NULL
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
    """Compute the column updates for one classified row.

    The llm_* columns record the verdict verbatim; the final action comes from
    models.finalize (agreement confirms the rule, disagreement -> review). The
    ephemeral flag is stored as the LLM's own judgment — the auto-trash gate's
    age-floor waiver requires BOTH rule_ephemeral and llm_ephemeral, so the
    AND-semantics live in the gate, not here.
    """
    action, source = finalize(row["rule_action"], result.action)
    return (
        result.action,
        result.category,
        result.confidence,
        result.reason,
        int(result.ephemeral),
        action,
        source,
        row["id"],
    )


_UPDATE_SQL = """
UPDATE messages
SET llm_action=?, llm_category=?, llm_confidence=?, llm_reason=?, llm_ephemeral=?,
    action=?, decision_source=?
WHERE id=?
"""

# Confidence 0.0 marks the row done-but-untrusted: action='review' lands it in
# human review, makes it resume-safe (non-NULL action drops it from
# re-selection), and can never pass the auto-approval gates.
_FAILURE_SQL = f"""
UPDATE messages
SET llm_action='{Action.REVIEW.value}', llm_confidence=0.0, llm_reason=?,
    action='{Action.REVIEW.value}', decision_source='{DecisionSource.LLM.value}'
WHERE id=?
"""


def classify_messages(
    conn: sqlite3.Connection,
    cfg: Config,
    chain: Runnable,
    limit: int | None = None,
    progress: Callable[[ClassifyStats, int], None] | None = None,
) -> ClassifyStats:
    rows = conn.execute(_SELECT_SQL).fetchall()
    if limit is not None:
        rows = rows[:limit]

    stats = ClassifyStats()

    def write_result(row: sqlite3.Row, result: EmailClassification | Exception) -> None:
        # Per-message write + progress: fires (on this thread) as each
        # in-flight request finalizes.
        if isinstance(result, Exception):
            logger.error("Giving up on message %s: %s", row["id"], result)
            conn.execute(
                _FAILURE_SQL, (f"classification failed: {result}", row["id"])
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
