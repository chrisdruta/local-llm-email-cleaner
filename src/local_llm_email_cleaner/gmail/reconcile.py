"""Reconcile local MBOX records against live Gmail before acting.

The Takeout export is stale by definition: messages may have been deleted,
moved, or the export may be months old. We therefore (1) search live Gmail by
RFC 822 Message-ID and (2) confirm the metadata matches before any mutation.
No confident match -> skip, never act.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from email.utils import parseaddr

from ..ingest.headers import normalize_addr, normalize_msgid

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileResult:
    gmail_api_id: str | None
    match_method: str  # 'rfc822msgid' | 'none'
    confirmed: bool
    detail: str


def reconcile_message(service, row: sqlite3.Row, execute_fn) -> ReconcileResult:
    """Resolve a local record to a live Gmail message id, confirming metadata.

    `execute_fn` wraps request.execute() with throttling/backoff (runner owns
    rate limiting policy).
    """
    # Preserve the Message-ID's case for the search — local-parts are
    # case-sensitive and Gmail's rfc822msgid: matches literally. Only the
    # later equality comparison is casefolded.
    rfc_id = normalize_msgid(row["rfc_message_id"])
    if rfc_id is None:
        return ReconcileResult(None, "none", False, "no Message-ID in local record")

    listing = execute_fn(
        service.users()
        .messages()
        .list(userId="me", q=f"rfc822msgid:{rfc_id}", maxResults=2)
    )
    candidates = listing.get("messages", [])
    if not candidates:
        return ReconcileResult(None, "rfc822msgid", False, "no live match in Gmail")
    if len(candidates) > 1:
        return ReconcileResult(
            None, "rfc822msgid", False, "ambiguous: multiple live matches"
        )

    gmail_api_id = candidates[0]["id"]
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
    headers = {
        h["name"].lower(): h["value"]
        for h in meta.get("payload", {}).get("headers", [])
    }

    live_msgid = normalize_msgid(headers.get("message-id"), casefold=True)
    if live_msgid != normalize_msgid(rfc_id, casefold=True):
        return ReconcileResult(
            gmail_api_id, "rfc822msgid", False, f"Message-ID mismatch: {live_msgid!r}"
        )

    # Parse the live From mailbox and compare normalized addresses for
    # equality — substring matching would confirm look-alike senders.
    local_from = row["from_addr"]
    live_from = normalize_addr(parseaddr(headers.get("from") or "")[1])
    if local_from and live_from and local_from != live_from:
        return ReconcileResult(
            gmail_api_id,
            "rfc822msgid",
            False,
            f"From mismatch: {headers.get('from')!r}",
        )

    return ReconcileResult(gmail_api_id, "rfc822msgid", True, "metadata confirmed")
