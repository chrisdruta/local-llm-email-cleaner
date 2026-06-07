"""Shared fixtures: a temp database and a synthetic Takeout-style MBOX."""

from __future__ import annotations

import dataclasses
import mailbox
import sqlite3
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest

from local_llm_email_cleaner import db
from local_llm_email_cleaner.config import DEFAULTS, Config

USER_ADDR = "user@example.com"
FRIEND_ADDR = "friend@example.com"

OLD_DATE = datetime(2019, 4, 1, 10, 0, tzinfo=UTC)
RECENT_DATE = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return dataclasses.replace(
        DEFAULTS,
        db_path=tmp_path / "test.db",
        user_addresses=(USER_ADDR,),
        requests_per_second=10_000,  # no throttling delays in tests
    )


@pytest.fixture
def conn(cfg: Config):
    connection = db.open_db(cfg.db_path)
    yield connection
    connection.close()


def _email(
    *,
    from_addr: str,
    to_addr: str = USER_ADDR,
    subject: str,
    body: str = "hello",
    date: datetime = OLD_DATE,
    message_id: str,
    labels: str | None = None,
    gm_msgid: str | None = None,
    gm_thrid: str | None = None,
    list_unsubscribe: bool = False,
    attachment: tuple[str, bytes] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date)
    msg["Message-ID"] = f"<{message_id}>"
    if labels:
        msg["X-Gmail-Labels"] = labels
    if gm_msgid:
        msg["X-GM-MSGID"] = gm_msgid
    if gm_thrid:
        msg["X-GM-THRID"] = gm_thrid
    if list_unsubscribe:
        msg["List-Unsubscribe"] = "<https://example.com/unsub>"
    msg.set_content(body)
    if attachment:
        name, payload = attachment
        msg.add_attachment(payload, maintype="image", subtype="png", filename=name)
    return msg


@pytest.fixture
def mbox_path(tmp_path: Path) -> Path:
    """Seven messages exercising every ingest/rules path."""
    path = tmp_path / "fixture.mbox"
    box = mailbox.mbox(str(path))

    # 1. Sent mail (user -> friend): drives known-contact derivation.
    box.add(
        _email(
            from_addr=USER_ADDR,
            to_addr=FRIEND_ADDR,
            subject="Re: dinner plans",
            body="see you saturday!",
            message_id="sent-1@example.com",
            labels="Sent",
        )
    )
    # 2. Incoming personal mail from that friend: protected as known contact.
    box.add(
        _email(
            from_addr=FRIEND_ADDR,
            subject="dinner was fun",
            body="let's do it again",
            message_id="friend-1@example.com",
            labels="Inbox",
        )
    )
    # 3. Old promo with Gmail identifiers and List-Unsubscribe.
    box.add(
        _email(
            from_addr="deals@shop.example",
            subject="HUGE SALE: 50% off everything",
            body="Limited time offer! Shop now.",
            message_id="promo-1@example.com",
            labels="Category Promotions,Unread",
            gm_msgid="1234567890123456789",
            gm_thrid="9876543210987654321",
            list_unsubscribe=True,
        )
    )
    # 4. Financial: protected by subject keywords.
    box.add(
        _email(
            from_addr="alerts@bank.example",
            subject="Your account statement is ready",
            body="Log in to view your statement.",
            message_id="bank-1@example.com",
        )
    )
    # 5. Unknown sender with attachment: no rule hits -> NEEDS_REVIEW.
    box.add(
        _email(
            from_addr="someone@unknown.example",
            subject="photos from the trip",
            body="attached!",
            message_id="photos-1@example.com",
            attachment=("img.png", b"\x89PNG fake"),
        )
    )
    # 6. Shipping notification from a noreply sender.
    box.add(
        _email(
            from_addr="noreply@store.example",
            subject="Your order has shipped!",
            body="Tracking inside.",
            message_id="ship-1@example.com",
        )
    )
    # 7. Recent promo (fails the policy gate's age condition).
    box.add(
        _email(
            from_addr="deals@shop.example",
            subject="Flash sale today only",
            body="Don't miss out.",
            message_id="promo-2@example.com",
            labels="Category Promotions",
            date=RECENT_DATE,
            list_unsubscribe=True,
        )
    )

    box.close()
    return path


def insert_message(conn: sqlite3.Connection, **overrides) -> int:
    """Insert a messages row with sensible defaults; returns its id."""
    row = {
        "gmail_msgid": None,
        "thread_id": None,
        "rfc_message_id": None,
        "labels": None,
        "date_utc": OLD_DATE.isoformat(),
        "date_epoch": int(OLD_DATE.timestamp()),
        "from_addr": "noreply@spam.example",
        "from_name": None,
        "from_domain": "spam.example",
        "to_addr": USER_ADDR,
        "to_all": USER_ADDR,
        "subject": "test message",
        "snippet": "body",
        "body_text": "body",
        "has_attachments": 0,
        "attachment_names": "[]",
        "size_bytes": 1000,
        "list_unsubscribe": 0,
        "ai_category": None,
        "ai_confidence": None,
        "ai_reason": None,
        "classified_by": None,
        "staged_label": None,
        "proposed_action": None,
        "review_status": "pending",
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join(f":{k}" for k in row)
    cur = conn.execute(f"INSERT INTO messages ({cols}) VALUES ({placeholders})", row)
    conn.commit()
    return cur.lastrowid


def add_rule_hit(
    conn: sqlite3.Connection, message_id: int, kind: str, name: str = "x"
) -> None:
    conn.execute(
        "INSERT INTO rule_hits (message_id, rule_name, rule_kind, outcome) VALUES (?, ?, ?, ?)",
        (message_id, name, kind, "DELETE_CANDIDATE" if kind == "candidate" else "KEEP"),
    )
    conn.commit()
