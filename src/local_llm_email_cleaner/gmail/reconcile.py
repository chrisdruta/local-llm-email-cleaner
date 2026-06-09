"""Reconcile local MBOX records against live Gmail before acting.

The Takeout export is stale by definition: messages may have been deleted,
moved, or the export may be months old. We therefore (1) search live Gmail —
by RFC 822 Message-ID when we have one, otherwise by sender/subject/date
metadata — and (2) confirm the metadata matches before any mutation. No
confident match -> skip, never act.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from email.utils import parseaddr

from ..ingest.headers import normalize_addr, normalize_msgid

logger = logging.getLogger(__name__)

# Date window (seconds) for the metadata fallback search — wide enough to
# absorb timezone/rounding slop between the export and live Gmail.
_METADATA_DATE_WINDOW = 2 * 86400


@dataclass(frozen=True)
class ReconcileResult:
    gmail_api_id: str | None
    match_method: str  # 'rfc822msgid' | 'metadata' | 'none'
    confirmed: bool
    detail: str


def _gmail_quote(value: str) -> str:
    """Quote a search-operator value so embedded whitespace or operators can't
    leak into the query as extra terms. Embedded double-quotes are dropped."""
    return '"' + value.replace('"', "") + '"'


def _confirmed_unique(
    listing: dict, method: str
) -> tuple[str | None, ReconcileResult | None]:
    """Resolve a `messages.list` response to a single id, or a skip result."""
    candidates = listing.get("messages", [])
    if not candidates:
        return None, ReconcileResult(None, method, False, "no live match in Gmail")
    if len(candidates) > 1:
        return None, ReconcileResult(
            None, method, False, "ambiguous: multiple live matches"
        )
    return candidates[0]["id"], None


def _fetch_headers(service, execute_fn, gmail_api_id: str) -> dict[str, str]:
    meta = execute_fn(
        service.users()
        .messages()
        .get(
            userId="me",
            id=gmail_api_id,
            format="metadata",
            metadataHeaders=["Message-ID", "From", "Subject"],
        )
    )
    return {
        h["name"].lower(): h["value"]
        for h in meta.get("payload", {}).get("headers", [])
    }


def _from_matches(row: sqlite3.Row, headers: dict[str, str]) -> bool:
    """Compare normalized From addresses for equality (substring matching would
    confirm look-alike senders). A live message with no parseable From fails."""
    local_from = row["from_addr"]
    live_from = normalize_addr(parseaddr(headers.get("from") or "")[1])
    return bool(local_from) and bool(live_from) and local_from == live_from


def _reconcile_by_msgid(
    service, row: sqlite3.Row, execute_fn, rfc_id: str
) -> ReconcileResult:
    # Preserve the Message-ID's case for the search — local-parts are
    # case-sensitive and Gmail's rfc822msgid: matches literally. Only the
    # later equality comparison is casefolded.
    listing = execute_fn(
        service.users()
        .messages()
        .list(userId="me", q=f"rfc822msgid:{_gmail_quote(rfc_id)}", maxResults=2)
    )
    gmail_api_id, skip = _confirmed_unique(listing, "rfc822msgid")
    if skip is not None:
        return skip

    headers = _fetch_headers(service, execute_fn, gmail_api_id)
    live_msgid = normalize_msgid(headers.get("message-id"), casefold=True)
    if live_msgid != normalize_msgid(rfc_id, casefold=True):
        return ReconcileResult(
            gmail_api_id, "rfc822msgid", False, f"Message-ID mismatch: {live_msgid!r}"
        )
    if not _from_matches(row, headers):
        return ReconcileResult(
            gmail_api_id,
            "rfc822msgid",
            False,
            f"From mismatch: {headers.get('from')!r}",
        )
    return ReconcileResult(gmail_api_id, "rfc822msgid", True, "metadata confirmed")


def _reconcile_by_metadata(service, row: sqlite3.Row, execute_fn) -> ReconcileResult:
    """Fallback for records with no RFC Message-ID: match on sender + subject
    within a date window, then confirm sender AND subject exactly. Strict on
    purpose — a fuzzy match here would trash the wrong message."""
    from_addr = row["from_addr"]
    subject = (row["subject"] or "").strip()
    date_epoch = row["date_epoch"]
    if not from_addr or not subject or date_epoch is None:
        return ReconcileResult(
            None, "none", False, "insufficient metadata for fallback"
        )

    q = (
        f"from:{_gmail_quote(from_addr)} subject:{_gmail_quote(subject)} "
        f"after:{date_epoch - _METADATA_DATE_WINDOW} "
        f"before:{date_epoch + _METADATA_DATE_WINDOW}"
    )
    listing = execute_fn(
        service.users().messages().list(userId="me", q=q, maxResults=2)
    )
    gmail_api_id, skip = _confirmed_unique(listing, "metadata")
    if skip is not None:
        return skip

    headers = _fetch_headers(service, execute_fn, gmail_api_id)
    if not _from_matches(row, headers):
        return ReconcileResult(
            gmail_api_id, "metadata", False, f"From mismatch: {headers.get('from')!r}"
        )
    if (headers.get("subject") or "").strip().casefold() != subject.casefold():
        return ReconcileResult(
            gmail_api_id,
            "metadata",
            False,
            f"Subject mismatch: {headers.get('subject')!r}",
        )
    return ReconcileResult(gmail_api_id, "metadata", True, "metadata confirmed")


def reconcile_message(service, row: sqlite3.Row, execute_fn) -> ReconcileResult:
    """Resolve a local record to a live Gmail message id, confirming metadata.

    `execute_fn` wraps request.execute() with throttling/backoff (runner owns
    rate limiting policy). Prefers the RFC Message-ID; falls back to a strict
    sender/subject/date metadata match when the record has none.
    """
    rfc_id = normalize_msgid(row["rfc_message_id"])
    if rfc_id is not None:
        return _reconcile_by_msgid(service, row, execute_fn, rfc_id)
    return _reconcile_by_metadata(service, row, execute_fn)
