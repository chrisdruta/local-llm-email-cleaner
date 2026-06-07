"""The deterministic action runner: executes only approved, logged actions.

Hard rules:
- consumes only rows with review_status in (approved, auto_approved)
- only users.messages.trash and users.messages.modify are ever called;
  users.messages.delete does not appear in this codebase
- every attempt (including dry runs) writes an `actions` audit row
- dry-run is the default; mutation requires execute=True
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from googleapiclient.errors import HttpError

from ..config import Config
from .reconcile import ReconcileResult, reconcile_message

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
RETRYABLE_HTTP = {429, 500, 502, 503}

_SELECT_APPROVED = """
SELECT id, gmail_msgid, rfc_message_id, from_addr, subject, proposed_action
FROM messages
WHERE review_status IN ('approved', 'auto_approved')
  AND proposed_action IN ('trash', 'archive')
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


def _record_action(
    conn: sqlite3.Connection,
    message_id: int,
    action: str,
    dry_run: bool,
    rec: ReconcileResult,
    status: str,
    http_status: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO actions (message_id, action, dry_run, reconciled, gmail_api_msgid,
                             match_method, match_confirmed, status, http_status, error,
                             completed_at)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            message_id,
            action,
            int(dry_run),
            rec.gmail_api_id,
            rec.match_method,
            int(rec.confirmed),
            status,
            http_status,
            error,
        ),
    )


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

    for row in rows:
        stats.examined += 1
        action = row["proposed_action"]
        try:
            rec = reconcile_message(service, row, executor)
        except HttpError as err:
            status_code = err.resp.status if err.resp else None
            _record_action(
                conn,
                row["id"],
                action,
                not execute,
                ReconcileResult(None, "none", False, "reconcile failed"),
                "error",
                status_code,
                str(err),
            )
            conn.commit()
            stats.errors += 1
            continue

        if not rec.confirmed:
            # Stale/ambiguous record: take it out of the queue, never act.
            _record_action(conn, row["id"], action, not execute, rec, "skipped")
            conn.execute(
                "UPDATE messages SET review_status='skipped', review_note=? WHERE id=?",
                (f"reconcile: {rec.detail}", row["id"]),
            )
            conn.commit()
            stats.note_skip(rec.detail)
            continue

        if not execute:
            _record_action(conn, row["id"], action, True, rec, "success")
            conn.commit()
            stats.succeeded += 1
            if progress:
                progress(stats)
            continue

        try:
            messages_api = service.users().messages()
            if action == "trash":
                executor(messages_api.trash(userId="me", id=rec.gmail_api_id))
            else:  # archive
                body: dict = {"removeLabelIds": ["INBOX"]}
                if cfg.archive_label:
                    if archive_label_id is None:
                        archive_label_id = _ensure_label(
                            service, executor, cfg.archive_label
                        )
                    body["addLabelIds"] = [archive_label_id]
                executor(
                    messages_api.modify(userId="me", id=rec.gmail_api_id, body=body)
                )
        except HttpError as err:
            status_code = err.resp.status if err.resp else None
            _record_action(
                conn, row["id"], action, False, rec, "error", status_code, str(err)
            )
            conn.commit()
            stats.errors += 1
            continue

        _record_action(conn, row["id"], action, False, rec, "success")
        conn.execute(
            "UPDATE messages SET review_status='applied' WHERE id=?", (row["id"],)
        )
        conn.commit()
        stats.succeeded += 1
        if progress:
            progress(stats)

    return stats
