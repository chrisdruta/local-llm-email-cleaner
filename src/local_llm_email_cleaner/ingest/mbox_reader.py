"""Streaming MBOX reader: yields one ParsedMessage at a time, never the whole file."""

from __future__ import annotations

import email
import logging
import mailbox
from collections.abc import Callable, Iterator
from email import policy
from pathlib import Path

from ..models import ParsedMessage
from . import bodies, headers

logger = logging.getLogger(__name__)


def parse_raw_message(raw: bytes) -> ParsedMessage:
    """Parse one raw RFC 822 message into a ParsedMessage."""
    msg = email.message_from_bytes(raw, policy=policy.compat32)

    gmail_msgid, thread_id, labels = headers.parse_gmail_headers(msg)
    from_addr, from_name = headers.parse_from(msg)
    recipients = headers.parse_recipients(msg)
    date_utc, date_epoch = headers.parse_date(msg)
    body_text, has_attachments, attachment_names = bodies.extract_body(msg)

    return ParsedMessage(
        gmail_msgid=gmail_msgid,
        thread_id=thread_id,
        rfc_message_id=headers.parse_message_id(msg),
        labels=labels,
        date_utc=date_utc,
        date_epoch=date_epoch,
        from_addr=from_addr,
        from_name=from_name,
        from_domain=headers.addr_domain(from_addr),
        to_addr=recipients[0] if recipients else None,
        to_all=",".join(recipients) if recipients else None,
        subject=headers.decode_str(msg.get("Subject")),
        body_text=body_text,
        has_attachments=has_attachments,
        attachment_names=attachment_names,
        size_bytes=len(raw),
        list_unsubscribe=msg.get("List-Unsubscribe") is not None,
    )


def iter_attachments(
    path: Path | str,
    wanted_message_ids: set[str],
    on_scan: Callable[[int, int], None] | None = None,
) -> Iterator[tuple[str, list[tuple[str, str, bytes]]]]:
    """Stream the MBOX, yielding (rfc_message_id, attachments) only for messages
    whose normalized Message-ID is in `wanted_message_ids`.

    Memory stays bounded to one message at a time. Attachment bytes are
    discarded at ingest, so this re-read is how voice-export recovers them;
    matching is by Message-ID (the only identifier these messages carry).
    `on_scan(scanned, total)` fires once per message scanned (for progress)."""
    if not wanted_message_ids:
        return
    box = mailbox.mbox(str(path), create=False)
    try:
        total = len(box)  # builds the offset table (the unavoidable full read)
        for scanned, key in enumerate(box.iterkeys(), 1):
            if on_scan is not None:
                on_scan(scanned, total)
            try:
                raw = box.get_bytes(key)
                msg = email.message_from_bytes(raw, policy=policy.compat32)
                mid = headers.parse_message_id(msg)
                if mid is None or mid not in wanted_message_ids:
                    continue
                yield mid, bodies.extract_attachments(msg)
            except Exception:
                logger.warning(
                    "Skipping unreadable message at mbox key %s", key, exc_info=True
                )
                continue
    finally:
        box.close()


def iter_mbox(path: Path | str, limit: int | None = None) -> Iterator[ParsedMessage]:
    """Stream messages from an MBOX file.

    `mailbox.mbox` builds an offset table, then we fetch each message's raw
    bytes individually — memory stays bounded to one message at a time.
    Unparseable messages are logged and skipped, never fatal.
    """
    box = mailbox.mbox(str(path), create=False)
    try:
        count = 0
        for key in box.iterkeys():
            if limit is not None and count >= limit:
                break
            try:
                raw = box.get_bytes(key)
                yield parse_raw_message(raw)
            except Exception:
                logger.warning(
                    "Skipping unparseable message at mbox key %s", key, exc_info=True
                )
                continue
            count += 1
    finally:
        box.close()
