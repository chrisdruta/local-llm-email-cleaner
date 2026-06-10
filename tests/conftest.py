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


def _write_raw_mbox(path: Path, raw_messages: list[bytes]) -> Path:
    """Write raw RFC822 byte blobs as an mbox, bypassing EmailMessage's
    normalization — needed for malformed/encoded fixtures a compliant
    generator refuses to produce."""
    with open(path, "wb") as f:
        for raw in raw_messages:
            f.write(b"From MAILER-DAEMON Mon Apr  1 10:00:00 2019\n")
            f.write(raw.rstrip(b"\n") + b"\n\n")
    return path


_EDGE_COMMON = b"To: user@example.com\nDate: Mon, 01 Apr 2019 10:00:00 +0000\n"

EDGE_MESSAGES: list[bytes] = [
    # 1. RFC2047-encoded subject and display name.
    (
        b"From: =?utf-8?q?Ren=C3=A9?= <rene@fr.example>\n"
        + _EDGE_COMMON
        + b"Subject: =?utf-8?q?Caf=C3=A9_offre_sp=C3=A9ciale?=\n"
        b"Message-ID: <rfc2047-1@example.com>\n"
        b"\nBonjour!\n"
    ),
    # 2. HTML-only body (no text/plain alternative).
    (
        b"From: promo@htmlonly.example\n" + _EDGE_COMMON + b"Subject: html only promo\n"
        b"Message-ID: <htmlonly-1@example.com>\n"
        b'Content-Type: text/html; charset="utf-8"\n'
        b"\n<html><body><p>Big <b>sale</b> on everything</p></body></html>\n"
    ),
    # 3. Nested multipart: mixed( alternative(plain, html), pdf attachment ).
    (
        b"From: nested@multi.example\n" + _EDGE_COMMON + b"Subject: nested multipart\n"
        b"Message-ID: <nested-1@example.com>\n"
        b'Content-Type: multipart/mixed; boundary="OUTER"\n'
        b"\n--OUTER\n"
        b'Content-Type: multipart/alternative; boundary="INNER"\n'
        b"\n--INNER\n"
        b'Content-Type: text/plain; charset="utf-8"\n'
        b"\nnested plain body\n"
        b"--INNER\n"
        b'Content-Type: text/html; charset="utf-8"\n'
        b"\n<p>nested html body</p>\n"
        b"--INNER--\n"
        b"--OUTER\n"
        b"Content-Type: application/pdf\n"
        b'Content-Disposition: attachment; filename="doc.pdf"\n'
        b"Content-Transfer-Encoding: base64\n"
        b"\nJVBERi0=\n"
        b"--OUTER--\n"
    ),
    # 4. Non-UTF8 charset (iso-8859-1) body with a high byte.
    (
        b"From: legacy@charset.example\n" + _EDGE_COMMON + b"Subject: latin-1 body\n"
        b"Message-ID: <latin1-1@example.com>\n"
        b'Content-Type: text/plain; charset="iso-8859-1"\n'
        b"Content-Transfer-Encoding: 8bit\n"
        b"\ncaf\xe9 au lait\n"
    ),
    # 5. Bogus charset label -> _decode_part's LookupError fallback.
    (
        b"From: bogus@charset.example\n" + _EDGE_COMMON + b"Subject: bogus charset\n"
        b"Message-ID: <bogus-charset-1@example.com>\n"
        b'Content-Type: text/plain; charset="not-a-charset"\n'
        b"\nweird charset body\n"
    ),
    # 6. No Message-ID at all, but X-GM-MSGID present.
    (
        b"From: noid@example.com\n" + _EDGE_COMMON + b"Subject: missing message id\n"
        b"X-GM-MSGID: 555000111222333444\n"
        b"\nno rfc message id here\n"
    ),
    # 7. Unclosed <style>: text after it must still be extracted.
    (
        b"From: promo@unclosed.example\n"
        + _EDGE_COMMON
        + b"Subject: unclosed style promo\n"
        b"Message-ID: <unclosed-style-1@example.com>\n"
        b'Content-Type: text/html; charset="utf-8"\n'
        b'\n<html><head><style type="text/css">.x{color:red}\n'
        b"</head><body>visible promo text</body></html>\n"
    ),
    # 8. text/csv part with a filename but NO Content-Disposition: a real
    #    attachment that must set has_attachments=1 (auto-trash gate input).
    (
        b"From: billing@invoices.example\n" + _EDGE_COMMON + b"Subject: your invoice\n"
        b"Message-ID: <csv-1@example.com>\n"
        b'Content-Type: multipart/mixed; boundary="CSVB"\n'
        b"\n--CSVB\n"
        b'Content-Type: text/plain; charset="utf-8"\n'
        b"\ninvoice attached\n"
        b"--CSVB\n"
        b'Content-Type: text/csv; name="invoice.csv"\n'
        b"\nitem,price\nwidget,9.99\n"
        b"--CSVB--\n"
    ),
    # 9. mbox From-quoting: a body line starting with '>From ' must neither
    #    split the mbox nor lose the line.
    (
        b"From: archivist@example.com\n" + _EDGE_COMMON + b"Subject: from-quoted body\n"
        b"Message-ID: <fromquote-1@example.com>\n"
        b"\nfirst line\n>From the archive, a quoted line\nlast line\n"
    ),
]


@pytest.fixture
def edge_mbox_path(tmp_path: Path) -> Path:
    """Real-Takeout-shaped edge cases the EmailMessage-built fixture can't express."""
    return _write_raw_mbox(tmp_path / "edge.mbox", EDGE_MESSAGES)


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
        "body_text": "body",
        "has_attachments": 0,
        "attachment_names": "[]",
        "size_bytes": 1000,
        "list_unsubscribe": 0,
        "ruled_at": None,
        "rule_name": None,
        "rule_action": None,
        "rule_category": None,
        "rule_protected": 0,
        "rule_ephemeral": 0,
        "llm_action": None,
        "llm_category": None,
        "llm_confidence": None,
        "llm_reason": None,
        "llm_ephemeral": 0,
        "action": None,
        "decision_source": None,
        "review_status": "pending",
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join(f":{k}" for k in row)
    cur = conn.execute(f"INSERT INTO messages ({cols}) VALUES ({placeholders})", row)
    conn.commit()
    return cur.lastrowid


def add_rule_hit(
    conn: sqlite3.Connection,
    message_id: int,
    action: str = "trash",
    name: str = "x",
    won: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO rule_hits (message_id, rule_name, action, won) VALUES (?, ?, ?, ?)",
        (message_id, name, action, int(won)),
    )
    conn.commit()


def make_ruleset(tmp_path: Path, toml_text: str):
    """Load a RuleSet from inline TOML (shared helper for rule-driven tests)."""
    from local_llm_email_cleaner.rules.ruleset import load_ruleset

    path = tmp_path / "inline_rules.toml"
    path.write_text(toml_text, encoding="utf-8")
    return load_ruleset(path)


@pytest.fixture
def default_ruleset():
    """The packaged starter rules.toml, parsed."""
    import tomllib
    from importlib import resources

    from local_llm_email_cleaner.rules.ruleset import RuleSet

    raw = tomllib.loads(
        resources.files("local_llm_email_cleaner")
        .joinpath("rules/default_rules.toml")
        .read_text(encoding="utf-8")
    )
    return RuleSet.model_validate(raw)
