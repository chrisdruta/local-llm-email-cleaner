"""The deterministic action runner: executes only approved, logged actions.

Hard rules:
- consumes only rows with review_status in (approved, auto_approved)
- only users.messages.trash and users.messages.batchModify are ever called;
  users.messages.delete does not appear in this codebase
- every attempt (including dry runs) writes an `actions` audit row
- dry-run is the default; mutation requires execute=True, and a dry run
  never changes review_status — only execute runs touch decision state
- write-intent-then-mark-success: an 'attempt' row is committed BEFORE each
  live mutation and finalized to success/error after. An 'attempt' row left
  with NULL completed_at means the process died mid-mutation — Gmail may or
  may not have applied it; the next run re-reconciles and retries.

Archive mutations are batched through users.messages.batchModify (identical
label delta, up to ARCHIVE_BATCH_SIZE ids per call); trash stays per-message
via users.messages.trash. A failed batch marks its whole chunk 'error' and
leaves review_status untouched, so the run is safely re-runnable.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from googleapiclient.errors import HttpError

from ..config import Config
from ..models import (
    ACTIONABLE_ACTIONS,
    APPROVABLE_STATUSES,
    ActionStatus,
    ProposedAction,
    ReviewStatus,
    sql_in_list,
)
from .reconcile import ReconcileResult, reconcile_message

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
RETRYABLE_HTTP = {429, 500, 502, 503}
ARCHIVE_BATCH_SIZE = 1000  # batchModify's documented per-call id limit

# Must stay in lockstep with review.queries.EXPORT_ACTIONS — both build the
# approved-actionable predicate from the same models constants.
_SELECT_APPROVED = f"""
SELECT id, gmail_msgid, rfc_message_id, from_addr, subject, proposed_action
FROM messages
WHERE review_status IN ({sql_in_list(APPROVABLE_STATUSES)})
  AND proposed_action IN ({sql_in_list(ACTIONABLE_ACTIONS)})
ORDER BY id
"""


@dataclass
class ApplyStats:
    examined: int = 0
    succeeded: int = 0
    skipped: int = 0
    errors: int = 0
    dry_run: bool = True
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def note_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


class Throttle:
    """Token-bucket-lite: enforce a minimum interval between API calls."""

    def __init__(self, requests_per_second: float) -> None:
        self._interval = 1.0 / max(requests_per_second, 0.1)
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delta = self._interval - (now - self._last)
        if delta > 0:
            time.sleep(delta)
        self._last = time.monotonic()


def _is_rate_limit(err: HttpError) -> bool:
    status = err.resp.status if err.resp else None
    if status in RETRYABLE_HTTP:
        return True
    return status == 403 and b"ratelimitexceeded" in (err.content or b"").lower()


def make_executor(throttle: Throttle) -> Callable:
    """Wrap request.execute() with throttling and backoff on rate limits."""

    def execute(request):
        delay = 2.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            throttle.wait()
            try:
                return request.execute()
            except HttpError as err:
                if attempt < MAX_ATTEMPTS and _is_rate_limit(err):
                    logger.warning(
                        "Rate limited (attempt %d); backing off %.0fs", attempt, delay
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise AssertionError("unreachable")

    return execute


def _ensure_label(service, executor: Callable, name: str) -> str:
    """Return the id of the user label `name`, creating the label if needed."""
    labels_api = service.users().labels()
    existing = executor(labels_api.list(userId="me")).get("labels", [])
    for label in existing:
        if label["name"].lower() == name.lower():
            return label["id"]
    created = executor(
        labels_api.create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
    )
    logger.info("Created Gmail label %r (%s)", name, created["id"])
    return created["id"]


def _insert_action(
    conn: sqlite3.Connection,
    message_id: int,
    action: str,
    dry_run: bool,
    rec: ReconcileResult,
    status: str,
    http_status: int | None = None,
    error: str | None = None,
    reconciled: bool = True,
) -> int:
    """Insert one audit row; returns its id.

    `reconciled` records whether reconcile_message ran to completion (the
    error path passes False); rec.confirmed lands in match_confirmed.
    completed_at stays NULL for ATTEMPT rows — _finalize_action stamps it.
    """
    terminal = status != ActionStatus.ATTEMPT.value
    cur = conn.execute(
        """
        INSERT INTO actions (message_id, action, dry_run, reconciled, gmail_api_msgid,
                             match_method, match_confirmed, status, http_status, error,
                             completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                CASE WHEN ? THEN datetime('now') END)
        """,
        (
            message_id,
            action,
            int(dry_run),
            int(reconciled),
            rec.gmail_api_id,
            rec.match_method,
            int(rec.confirmed),
            status,
            http_status,
            error,
            int(terminal),
        ),
    )
    return cur.lastrowid


def _finalize_action(
    conn: sqlite3.Connection,
    action_id: int,
    status: str,
    http_status: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE actions SET status=?, http_status=?, error=?, completed_at=datetime('now')
        WHERE id=?
        """,
        (status, http_status, error, action_id),
    )


def _http_status(err: HttpError) -> int | None:
    return err.resp.status if err.resp else None


def apply_actions(
    conn: sqlite3.Connection,
    cfg: Config,
    service,
    execute: bool = False,
    limit: int | None = None,
    progress: Callable[[ApplyStats], None] | None = None,
) -> ApplyStats:
    rows = conn.execute(_SELECT_APPROVED).fetchall()
    if limit is not None:
        rows = rows[:limit]

    stats = ApplyStats(dry_run=not execute)
    executor = make_executor(Throttle(cfg.requests_per_second))
    archive_label_id: str | None = None  # resolved lazily on the first real archive
    # Confirmed archives buffered for batchModify: (message_id, gmail_api_id,
    # actions-row id). Intent rows are committed before buffering, so a crash
    # between buffer and flush leaves visible 'attempt' rows.
    pending_archive: list[tuple[int, str, int]] = []

    def flush_archive() -> None:
        if not pending_archive:
            return
        body: dict = {
            "ids": [gmail_id for _, gmail_id, _ in pending_archive],
            "removeLabelIds": ["INBOX"],
        }
        if archive_label_id is not None:
            body["addLabelIds"] = [archive_label_id]
        try:
            executor(service.users().messages().batchModify(userId="me", body=body))
        except HttpError as err:
            # Whole chunk fails together; review_status stays approved, so a
            # re-run re-reconciles and retries these messages.
            for _, _, action_id in pending_archive:
                _finalize_action(
                    conn,
                    action_id,
                    ActionStatus.ERROR.value,
                    _http_status(err),
                    str(err),
                )
            conn.commit()
            stats.errors += len(pending_archive)
            pending_archive.clear()
            if progress:
                progress(stats)
            return
        for _, _, action_id in pending_archive:
            _finalize_action(conn, action_id, ActionStatus.SUCCESS.value)
        conn.executemany(
            f"UPDATE messages SET review_status='{ReviewStatus.APPLIED.value}' WHERE id=?",
            [(message_id,) for message_id, _, _ in pending_archive],
        )
        conn.commit()
        stats.succeeded += len(pending_archive)
        pending_archive.clear()
        if progress:
            progress(stats)

    for row in rows:
        stats.examined += 1
        action = row["proposed_action"]
        try:
            rec = reconcile_message(service, row, executor)
        except HttpError as err:
            _insert_action(
                conn,
                row["id"],
                action,
                not execute,
                ReconcileResult(None, "none", False, "reconcile failed"),
                ActionStatus.ERROR.value,
                _http_status(err),
                str(err),
                reconciled=False,
            )
            conn.commit()
            stats.errors += 1
            continue

        if not rec.confirmed:
            # Stale/ambiguous record: never act. Only a real run takes it out
            # of the queue — a dry run must not change decision state.
            _insert_action(
                conn, row["id"], action, not execute, rec, ActionStatus.SKIPPED.value
            )
            if execute:
                conn.execute(
                    f"UPDATE messages SET review_status='{ReviewStatus.SKIPPED.value}', "
                    "review_note=? WHERE id=?",
                    (f"reconcile: {rec.detail}", row["id"]),
                )
            conn.commit()
            stats.note_skip(rec.detail)
            continue

        if not execute:
            _insert_action(
                conn, row["id"], action, True, rec, ActionStatus.SUCCESS.value
            )
            conn.commit()
            stats.succeeded += 1
            if progress:
                progress(stats)
            continue

        if action == ProposedAction.TRASH.value:
            action_id = _insert_action(
                conn, row["id"], action, False, rec, ActionStatus.ATTEMPT.value
            )
            conn.commit()  # durable intent before the mutation
            try:
                executor(
                    service.users().messages().trash(userId="me", id=rec.gmail_api_id)
                )
            except HttpError as err:
                _finalize_action(
                    conn,
                    action_id,
                    ActionStatus.ERROR.value,
                    _http_status(err),
                    str(err),
                )
                conn.commit()
                stats.errors += 1
                continue
            _finalize_action(conn, action_id, ActionStatus.SUCCESS.value)
            conn.execute(
                f"UPDATE messages SET review_status='{ReviewStatus.APPLIED.value}' WHERE id=?",
                (row["id"],),
            )
            conn.commit()
            stats.succeeded += 1
            if progress:
                progress(stats)
        elif action == ProposedAction.ARCHIVE.value:
            # Resolve the label before writing the intent row so a label
            # failure doesn't leave a dangling attempt.
            if cfg.archive_label and archive_label_id is None:
                try:
                    archive_label_id = _ensure_label(
                        service, executor, cfg.archive_label
                    )
                except HttpError as err:
                    _insert_action(
                        conn,
                        row["id"],
                        action,
                        False,
                        rec,
                        ActionStatus.ERROR.value,
                        _http_status(err),
                        str(err),
                    )
                    conn.commit()
                    stats.errors += 1
                    continue
            action_id = _insert_action(
                conn, row["id"], action, False, rec, ActionStatus.ATTEMPT.value
            )
            conn.commit()  # durable intent before the (batched) mutation
            pending_archive.append((row["id"], rec.gmail_api_id, action_id))
            if len(pending_archive) >= ARCHIVE_BATCH_SIZE:
                flush_archive()
        else:
            # _SELECT_APPROVED filters to ACTIONABLE_ACTIONS; anything else
            # reaching here is a programming error, not data to act on.
            raise ValueError(f"unexpected proposed_action {action!r}")

    flush_archive()
    return stats
