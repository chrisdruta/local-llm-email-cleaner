"""Reconcile local MBOX records against live Gmail before acting.

The Takeout export is stale by definition: messages may have been deleted,
moved, or the export may be months old. We therefore (1) search live Gmail —
by RFC 822 Message-ID when we have one, otherwise by sender/subject/date
metadata — and (2) confirm the metadata matches before any mutation. No
confident match -> skip, never act.

Reconciliation is two API steps: a `list` search (one per record) and a
metadata `get`. `reconcile_chunk` batches the gets for a whole chunk into a
single HTTP request, halving the round-trips of a large apply run.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from email.utils import parseaddr

from ..ingest.headers import normalize_addr, normalize_msgid

logger = logging.getLogger(__name__)

_METADATA_HEADERS = ["Message-ID", "From", "Subject"]

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


def _get_request(service, gmail_api_id: str):
    return (
        service.users()
        .messages()
        .get(
            userId="me",
            id=gmail_api_id,
            format="metadata",
            metadataHeaders=_METADATA_HEADERS,
        )
    )


def _headers_of(meta: dict) -> dict[str, str]:
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


def _search(
    service, row: sqlite3.Row, execute_fn
) -> tuple[str, str | None, ReconcileResult | None]:
    """Phase 1: resolve a record to one live candidate id via a `list` search.

    Returns (method, candidate_id, early_result). `early_result` is set (with a
    None candidate) when the search already decides the outcome — no match,
    ambiguous, or insufficient metadata for the fallback.
    """
    rfc_id = normalize_msgid(row["rfc_message_id"])
    if rfc_id is not None:
        # Preserve the Message-ID's case — local-parts are case-sensitive and
        # rfc822msgid: matches literally; only comparison is casefolded.
        listing = execute_fn(
            service.users()
            .messages()
            .list(userId="me", q=f"rfc822msgid:{_gmail_quote(rfc_id)}", maxResults=2)
        )
        candidate, early = _confirmed_unique(listing, "rfc822msgid")
        return "rfc822msgid", candidate, early

    from_addr = row["from_addr"]
    subject = (row["subject"] or "").strip()
    date_epoch = row["date_epoch"]
    if not from_addr or not subject or date_epoch is None:
        return (
            "metadata",
            None,
            ReconcileResult(None, "none", False, "insufficient metadata for fallback"),
        )
    q = (
        f"from:{_gmail_quote(from_addr)} subject:{_gmail_quote(subject)} "
        f"after:{date_epoch - _METADATA_DATE_WINDOW} "
        f"before:{date_epoch + _METADATA_DATE_WINDOW}"
    )
    listing = execute_fn(
        service.users().messages().list(userId="me", q=q, maxResults=2)
    )
    candidate, early = _confirmed_unique(listing, "metadata")
    return "metadata", candidate, early


def _confirm(
    row: sqlite3.Row, method: str, gmail_api_id: str, headers: dict[str, str]
) -> ReconcileResult:
    """Phase 3: confirm the live message's metadata matches the local record."""
    if method == "rfc822msgid":
        rfc_id = normalize_msgid(row["rfc_message_id"])
        live_msgid = normalize_msgid(headers.get("message-id"), casefold=True)
        if live_msgid != normalize_msgid(rfc_id, casefold=True):
            return ReconcileResult(
                gmail_api_id, method, False, f"Message-ID mismatch: {live_msgid!r}"
            )
        if not _from_matches(row, headers):
            return ReconcileResult(
                gmail_api_id, method, False, f"From mismatch: {headers.get('from')!r}"
            )
        return ReconcileResult(gmail_api_id, method, True, "metadata confirmed")

    # Metadata fallback: no Message-ID to anchor on, so require sender AND
    # subject to match exactly.
    subject = (row["subject"] or "").strip()
    if not _from_matches(row, headers):
        return ReconcileResult(
            gmail_api_id, method, False, f"From mismatch: {headers.get('from')!r}"
        )
    if (headers.get("subject") or "").strip().casefold() != subject.casefold():
        return ReconcileResult(
            gmail_api_id, method, False, f"Subject mismatch: {headers.get('subject')!r}"
        )
    return ReconcileResult(gmail_api_id, method, True, "metadata confirmed")


def reconcile_message(service, row: sqlite3.Row, execute_fn) -> ReconcileResult:
    """Resolve a single local record to a live Gmail message id, confirming
    metadata. Prefers the RFC Message-ID; falls back to a strict
    sender/subject/date metadata match when the record has none."""
    method, candidate, early = _search(service, row, execute_fn)
    if early is not None:
        return early
    headers = _headers_of(execute_fn(_get_request(service, candidate)))
    return _confirm(row, method, candidate, headers)


def _batch_get_headers(
    service, requests: list[tuple[str, str]], execute_fn
) -> tuple[dict[str, dict[str, str]], dict[str, Exception]]:
    """Fetch metadata for many candidates in ONE batched HTTP request.

    `requests` is (request_key, gmail_api_id) pairs; keys are returned in the
    headers/errors maps. A whole-batch failure (after the executor's retries)
    marks every request as errored — never a silent skip.
    """
    headers: dict[str, dict[str, str]] = {}
    errors: dict[str, Exception] = {}
    if not requests:
        return headers, errors

    def callback(request_id, response, exception):
        if exception is not None:
            errors[request_id] = exception
        else:
            headers[request_id] = _headers_of(response)

    batch = service.new_batch_http_request(callback=callback)
    for key, gmail_api_id in requests:
        batch.add(_get_request(service, gmail_api_id), request_id=key)
    try:
        execute_fn(batch)
    except Exception as err:  # whole-batch transport failure after retries
        for key, _ in requests:
            errors.setdefault(key, err)
    return headers, errors


def reconcile_chunk(
    service, rows: list[sqlite3.Row], execute_fn
) -> list[tuple[sqlite3.Row, ReconcileResult | None, Exception | None]]:
    """Reconcile a chunk of records, batching the metadata `get`s into a single
    HTTP request. Returns (row, result, error) per input row, in order: exactly
    one of result/error is set. A list-search or get failure surfaces as the
    error so the runner records a re-runnable error, never a silent skip.
    """
    # Phase 1: one `list` search per row (search can't be cleanly batched).
    searched: list[list] = []
    for row in rows:
        try:
            method, candidate, early = _search(service, row, execute_fn)
            searched.append([row, method, candidate, early, None])
        except Exception as err:  # noqa: BLE001 - recorded per-row, run continues
            searched.append([row, None, None, None, err])

    # Phase 2: one batched `get` for every row that resolved to a candidate.
    # Key by row id (distinct rows may resolve to the same live id).
    pending = [s for s in searched if s[4] is None and s[3] is None]
    headers, get_errors = _batch_get_headers(
        service, [(str(s[0]["id"]), s[2]) for s in pending], execute_fn
    )

    # Phase 3: confirm each resolved candidate against its fetched metadata.
    out: list[tuple[sqlite3.Row, ReconcileResult | None, Exception | None]] = []
    for row, method, candidate, early, err in searched:
        key = str(row["id"])
        if err is not None:
            out.append((row, None, err))
        elif early is not None:
            out.append((row, early, None))
        elif key in get_errors:
            out.append((row, None, get_errors[key]))
        else:
            out.append(
                (row, _confirm(row, method, candidate, headers.get(key, {})), None)
            )
    return out
