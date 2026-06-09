"""Header extraction and normalization for Takeout MBOX messages."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

logger = logging.getLogger(__name__)

GMAIL_MSGID_HEADER = "X-GM-MSGID"
GMAIL_THRID_HEADER = "X-GM-THRID"
GMAIL_LABELS_HEADER = "X-Gmail-Labels"


def decode_str(value: object) -> str | None:
    """Best-effort decode of a possibly RFC2047-encoded header value."""
    if value is None:
        return None
    try:
        text = str(make_header(decode_header(str(value))))
    except Exception:  # malformed encoded-words are common in old mail
        text = str(value)
    return " ".join(text.split()) or None


def normalize_addr(addr: str | None) -> str | None:
    if not addr:
        return None
    addr = addr.strip().strip("<>").lower()
    return addr if "@" in addr else None


def addr_domain(addr: str | None) -> str | None:
    if not addr or "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1] or None


def parse_from(msg: Message) -> tuple[str | None, str | None]:
    """Return (normalized address, display name) of the first From mailbox."""
    pairs = getaddresses([str(msg.get("From", ""))])
    for name, addr in pairs:
        normalized = normalize_addr(addr)
        if normalized:
            return normalized, decode_str(name)
    return None, None


def parse_recipients(msg: Message) -> list[str]:
    """All normalized To/Cc addresses, deduplicated, in order."""
    raw = [str(v) for field in ("To", "Cc") for v in msg.get_all(field, [])]
    seen: dict[str, None] = {}
    for _, addr in getaddresses(raw):
        normalized = normalize_addr(addr)
        if normalized:
            seen.setdefault(normalized)
    return list(seen)


def parse_date(msg: Message) -> tuple[str | None, int | None]:
    """Return (ISO-8601 UTC string, unix epoch) from the Date header."""
    raw = msg.get("Date")
    if not raw:
        return None, None
    try:
        dt = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.isoformat(), int(dt.timestamp())


def normalize_msgid(raw: object, *, casefold: bool = False) -> str | None:
    """Strip whitespace and angle brackets from an RFC 822 Message-ID.

    Case is preserved by default — Message-ID local-parts are case-sensitive
    and Gmail's rfc822msgid: search matches literally. Pass casefold=True
    only for case-insensitive *comparison*, never for storage or queries.
    """
    if raw is None:
        return None
    value = str(raw).strip().strip("<>").strip()
    if casefold:
        value = value.casefold()
    return value or None


def parse_message_id(msg: Message) -> str | None:
    return normalize_msgid(msg.get("Message-ID") or msg.get("Message-Id"))


def parse_gmail_headers(msg: Message) -> tuple[str | None, str | None, str | None]:
    """Return (gmail_msgid, thread_id, labels) from Takeout's X-GM-*/X-Gmail-* headers."""
    msgid = msg.get(GMAIL_MSGID_HEADER)
    thrid = msg.get(GMAIL_THRID_HEADER)
    labels = decode_str(msg.get(GMAIL_LABELS_HEADER))
    return (
        str(msgid).strip() if msgid else None,
        str(thrid).strip() if thrid else None,
        labels,
    )


def parse_epoch_to_age_cutoff(months: int, now: datetime | None = None) -> int:
    """Unix epoch for `months` ago; messages older than this pass age filters."""
    now = now or datetime.now(UTC)
    # Approximate months as 30.44 days — precision is irrelevant for cleanup thresholds.
    return int(now.timestamp() - months * 30.44 * 86400)


def parse_epoch_to_age_cutoff_days(days: int, now: datetime | None = None) -> int:
    """Unix epoch for `days` ago; messages older than this pass age filters.

    The fine-grained sibling of parse_epoch_to_age_cutoff, used for the short
    ephemeral-digest grace period (policy.py).
    """
    now = now or datetime.now(UTC)
    return int(now.timestamp() - days * 86400)
