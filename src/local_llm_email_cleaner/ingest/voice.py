"""Parse Google-Voice-style SMS / call-log emails into normalized records.

The mbox these come from was produced by a converter that writes one email
per SMS message and one per call, with a synthetic ``<number>@unknown.email``
From address for the other party. The shapes (confirmed against real data):

    SMS, inbound   From: "Michael" <+12164969651@unknown.email>
                   Subject: "SMS with Michael"
                   Body: the message text (single message, no speaker prefix)
    SMS, outbound  From: you@gmail.com           (a real address = you sent it)
                   Subject: "SMS with Michael"
                   Body: the message text
    Call           From: "<num>" <4408795640@unknown.email>
                   Subject: "Call with 4408795640"
                   Body: "23s (00:00:23)\n4408795640 (incoming call)"

Direction therefore falls out of the From domain: the converter only ever uses
``unknown.email`` for the *other* party, so a real-domain sender is you
(outbound). Calls additionally carry their direction in the body's
``(incoming|outgoing|missed call)`` tag.

This module is pure (row in -> dataclass out); persistence and file writing
live in ``voice_export``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from ..models import split_labels

#: Synthetic domain the converter assigns to the non-user party. A From address
#: on this domain is inbound; any other (real) domain means the user sent it.
UNKNOWN_DOMAIN = "unknown.email"

#: X-Gmail-Labels (lowercased) that mark each Google Voice message kind.
SMS_LABELS = frozenset({"sms"})
CALL_LABELS = frozenset({"call log"})
VOICEMAIL_LABELS = frozenset({"voicemail"})

KIND_SMS = "sms"
KIND_CALL = "call"
KIND_VOICEMAIL = "voicemail"

DIRECTION_INBOUND = "inbound"
DIRECTION_OUTBOUND = "outbound"
DIRECTION_MISSED = "missed"
DIRECTION_UNKNOWN = "unknown"

_SUBJECT_SMS_RE = re.compile(r"^\s*SMS with\s+(.+?)\s*$", re.IGNORECASE)
_SUBJECT_CALL_RE = re.compile(r"^\s*Call with\s+(.+?)\s*$", re.IGNORECASE)
_SUBJECT_VOICEMAIL_RE = re.compile(
    r"^\s*Voicemail (?:with|from)\s+(.+?)\s*$", re.IGNORECASE
)

#: call body: a "(HH:MM:SS)" run-time anywhere, and a "(<type> call)" tag.
_DURATION_RE = re.compile(r"\((\d{1,2}):(\d{2}):(\d{2})\)")
_CALL_TYPE_RE = re.compile(
    r"\((incoming|outgoing|placed|received|missed)\s+call\)", re.IGNORECASE
)

_CALL_TYPE_DIRECTION = {
    "incoming": DIRECTION_INBOUND,
    "received": DIRECTION_INBOUND,
    "outgoing": DIRECTION_OUTBOUND,
    "placed": DIRECTION_OUTBOUND,
    "missed": DIRECTION_MISSED,
}

#: a contact label that is purely a phone number (digits, +, spaces, dashes)
_NUMERIC_LABEL_RE = re.compile(r"^[\d+][\d\s().+-]*$")


@dataclass(frozen=True)
class VoiceMessage:
    """One normalized SMS / call / voicemail record."""

    message_id: int  # messages.id
    rfc_message_id: str | None
    thread_id: str | None
    kind: str  # KIND_*
    direction: str  # DIRECTION_*
    timestamp: str | None  # ISO-8601 UTC (messages.date_utc)
    epoch: int | None  # messages.date_epoch, for stable ordering
    contact_key: str  # case-folded grouping key (the subject contact)
    contact_name: str | None  # display name, None when only a number is known
    contact_number: str | None  # best-effort E.164-ish number
    text: str | None  # SMS body / voicemail transcript
    duration_seconds: int | None  # calls only
    call_type: str | None  # raw "(... call)" word, calls only
    has_attachments: bool  # MMS images etc. (bytes live only in the source mbox)
    attachment_names: tuple[str, ...]  # filenames recorded at ingest


def classify_kind(labels: frozenset[str]) -> str | None:
    """Which Google Voice kind these labels denote, or None if not one."""
    if labels & SMS_LABELS:
        return KIND_SMS
    if labels & VOICEMAIL_LABELS:
        return KIND_VOICEMAIL
    if labels & CALL_LABELS:
        return KIND_CALL
    return None


def normalize_number(raw: str | None) -> str | None:
    """Best-effort E.164-ish normalization. Leaves short codes / non-NANP
    strings untouched rather than guessing wrong."""
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", raw)
    if not digits:
        return None
    if digits.startswith("+"):
        return digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    # Plain 10-digit only assumed US when it starts with a valid NANP area-code
    # digit (2-9); sequences like "1410200646" are left as-is.
    if len(digits) == 10 and digits[0] in "23456789":
        return "+1" + digits
    return digits


def _local_part(addr: str | None) -> str | None:
    if not addr or "@" not in addr:
        return None
    return addr.rsplit("@", 1)[0] or None


def _optional(row: sqlite3.Row, key: str):
    """Column value, or None if this row's SELECT didn't include it."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _attachments(row: sqlite3.Row) -> tuple[bool, tuple[str, ...]]:
    has = bool(_optional(row, "has_attachments"))
    raw = _optional(row, "attachment_names")
    if not raw:
        return has, ()
    try:
        names = json.loads(raw)
    except (ValueError, TypeError):
        return has, ()
    return has, tuple(str(n) for n in names if n)


def _is_numeric_label(label: str | None) -> bool:
    return bool(label and _NUMERIC_LABEL_RE.match(label.strip()))


def _split_name_number(contact_label: str) -> tuple[str | None, str | None]:
    """A subject contact is either a human name or a bare number."""
    label = contact_label.strip()
    if _is_numeric_label(label):
        return None, normalize_number(label)
    return label or None, None


def parse_duration(body: str) -> int | None:
    m = _DURATION_RE.search(body)
    if not m:
        return None
    h, mnt, s = (int(g) for g in m.groups())
    return h * 3600 + mnt * 60 + s


def _from_is_inbound(from_addr: str | None) -> bool:
    """Inbound iff the From sits on the converter's synthetic domain; a real
    domain means the user sent the message (outbound)."""
    if not from_addr or "@" not in from_addr:
        return True  # no real sender info -> treat as the other party
    return from_addr.rsplit("@", 1)[1].lower() == UNKNOWN_DOMAIN


def parse_message(row: sqlite3.Row) -> VoiceMessage | None:
    """Parse one messages row into a VoiceMessage, or None if it isn't a
    Google Voice message. Never raises on malformed content."""
    kind = classify_kind(split_labels(row["labels"]))
    if kind is None:
        return None
    if kind == KIND_CALL:
        return _parse_call(row)
    if kind == KIND_VOICEMAIL:
        return _parse_voicemail(row)
    return _parse_sms(row)


def _subject_contact(subject: str | None, pattern: re.Pattern[str]) -> str | None:
    if not subject:
        return None
    m = pattern.match(subject)
    return m.group(1).strip() if m else None


def _contact_fields(
    subject_contact: str | None,
    from_name: str | None,
    from_addr: str | None,
    inbound: bool,
) -> tuple[str, str | None, str | None]:
    """Resolve (grouping key, display name, number) for a message.

    The subject contact ("SMS with X") is the most consistent join key the
    converter gives us — it is identical for inbound and outbound messages of
    the same conversation — so it anchors grouping. Inbound messages also carry
    the number as the From local-part."""
    label = subject_contact or from_name or _local_part(from_addr) or "unknown"
    name, number = _split_name_number(label)
    if name is None and from_name and not _is_numeric_label(from_name):
        name = from_name.strip()
    if inbound and number is None:
        number = normalize_number(_local_part(from_addr))
    return label.casefold(), name, number


def _parse_sms(row: sqlite3.Row) -> VoiceMessage:
    inbound = _from_is_inbound(row["from_addr"])
    subject_contact = _subject_contact(row["subject"], _SUBJECT_SMS_RE)
    key, name, number = _contact_fields(
        subject_contact, row["from_name"], row["from_addr"], inbound
    )
    has_attach, names = _attachments(row)
    return VoiceMessage(
        message_id=row["id"],
        rfc_message_id=row["rfc_message_id"],
        thread_id=row["thread_id"],
        kind=KIND_SMS,
        direction=DIRECTION_INBOUND if inbound else DIRECTION_OUTBOUND,
        timestamp=row["date_utc"],
        epoch=row["date_epoch"],
        contact_key=key,
        contact_name=name,
        contact_number=number,
        text=row["body_text"] or "",
        duration_seconds=None,
        call_type=None,
        has_attachments=has_attach,
        attachment_names=names,
    )


def _parse_call(row: sqlite3.Row) -> VoiceMessage:
    body = row["body_text"] or ""
    type_match = _CALL_TYPE_RE.search(body)
    call_type = type_match.group(1).lower() if type_match else None
    direction = _CALL_TYPE_DIRECTION.get(call_type, DIRECTION_UNKNOWN)

    subject_contact = _subject_contact(row["subject"], _SUBJECT_CALL_RE)
    # Calls are not conversational; prefer the body/subject number, then From.
    key, name, number = _contact_fields(
        subject_contact, row["from_name"], row["from_addr"], inbound=True
    )
    return VoiceMessage(
        message_id=row["id"],
        rfc_message_id=row["rfc_message_id"],
        thread_id=row["thread_id"],
        kind=KIND_CALL,
        direction=direction,
        timestamp=row["date_utc"],
        epoch=row["date_epoch"],
        contact_key=key,
        contact_name=name,
        contact_number=number,
        text=None,
        duration_seconds=parse_duration(body),
        call_type=call_type,
        has_attachments=False,
        attachment_names=(),
    )


def _parse_voicemail(row: sqlite3.Row) -> VoiceMessage:
    # No voicemail messages exist in the known corpus; this path is defensive.
    inbound = _from_is_inbound(row["from_addr"])
    subject_contact = _subject_contact(row["subject"], _SUBJECT_VOICEMAIL_RE)
    key, name, number = _contact_fields(
        subject_contact, row["from_name"], row["from_addr"], inbound
    )
    body = row["body_text"] or ""
    has_attach, names = _attachments(row)
    return VoiceMessage(
        message_id=row["id"],
        rfc_message_id=row["rfc_message_id"],
        thread_id=row["thread_id"],
        kind=KIND_VOICEMAIL,
        direction=DIRECTION_INBOUND if inbound else DIRECTION_OUTBOUND,
        timestamp=row["date_utc"],
        epoch=row["date_epoch"],
        contact_key=key,
        contact_name=name,
        contact_number=number,
        text=body,
        duration_seconds=parse_duration(body),
        call_type=None,
        has_attachments=has_attach,
        attachment_names=names,
    )
