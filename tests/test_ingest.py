"""Ingest: streaming parse, identifier preservation, idempotency, FTS, contacts."""

from __future__ import annotations

import json

import pytest
from conftest import FRIEND_ADDR, USER_ADDR

from local_llm_email_cleaner import db
from local_llm_email_cleaner.ingest import contacts, store


def test_schema_version_mismatch_raises(cfg):
    connection = db.open_db(cfg.db_path)
    connection.execute(
        "UPDATE schema_version SET version = ?", (db.SCHEMA_VERSION - 1,)
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="Re-run"):
        db.open_db(cfg.db_path)


def test_ingest_populates_messages(conn, mbox_path):
    stats = store.ingest_mbox(conn, mbox_path)
    assert stats.seen == 7
    assert stats.inserted == 7

    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert total == 7


def test_reingest_is_idempotent(conn, mbox_path):
    store.ingest_mbox(conn, mbox_path)
    stats = store.ingest_mbox(conn, mbox_path)
    assert stats.seen == 7
    assert stats.inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 7


def test_gmail_identifiers_preserved(conn, mbox_path):
    store.ingest_mbox(conn, mbox_path)
    row = conn.execute(
        "SELECT * FROM messages WHERE rfc_message_id='promo-1@example.com'"
    ).fetchone()
    assert row["gmail_msgid"] == "1234567890123456789"
    assert row["thread_id"] == "9876543210987654321"
    assert "category promotions" in row["labels"].lower()
    assert row["list_unsubscribe"] == 1
    assert row["from_domain"] == "shop.example"
    assert row["date_utc"].startswith("2019-04-01")
    assert row["size_bytes"] > 0


def test_attachment_extraction(conn, mbox_path):
    store.ingest_mbox(conn, mbox_path)
    row = conn.execute(
        "SELECT * FROM messages WHERE rfc_message_id='photos-1@example.com'"
    ).fetchone()
    assert row["has_attachments"] == 1
    assert json.loads(row["attachment_names"]) == ["img.png"]
    assert "attached!" in row["body_text"]

    no_attach = conn.execute(
        "SELECT has_attachments FROM messages WHERE rfc_message_id='friend-1@example.com'"
    ).fetchone()
    assert no_attach["has_attachments"] == 0


def test_fts_search(conn, mbox_path):
    store.ingest_mbox(conn, mbox_path)
    hits = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'sale'"
    ).fetchall()
    assert len(hits) == 2  # both promos mention "sale" in the subject


def test_contacts_derived_from_sent_mail(conn, mbox_path, cfg):
    store.ingest_mbox(conn, mbox_path)
    n = contacts.derive_contacts(conn, cfg.user_addresses)
    assert n == 1
    row = conn.execute(
        "SELECT * FROM contacts WHERE address=?", (FRIEND_ADDR,)
    ).fetchone()
    assert row is not None
    assert row["sent_count"] == 1
    # The user's own address never becomes a "contact".
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE address=?", (USER_ADDR,)
        ).fetchone()[0]
        == 0
    )


def test_contacts_skipped_without_user_addresses(conn, mbox_path):
    store.ingest_mbox(conn, mbox_path)
    assert contacts.derive_contacts(conn, ()) == 0


def test_contacts_derived_with_unnormalized_user_address(conn, mbox_path):
    """Display-form / mixed-case config must still match Sent mail."""
    store.ingest_mbox(conn, mbox_path)
    n = contacts.derive_contacts(conn, (f"Me <{USER_ADDR.upper()}>",))
    assert n == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE address=?", (FRIEND_ADDR,)
        ).fetchone()[0]
        == 1
    )


# --- Takeout-shaped edge cases (raw-bytes fixture) ---------------------------


def _edge_row(conn, rfc_message_id):
    return conn.execute(
        "SELECT * FROM messages WHERE rfc_message_id=?", (rfc_message_id,)
    ).fetchone()


def test_edge_mbox_all_messages_parse(conn, edge_mbox_path):
    stats = store.ingest_mbox(conn, edge_mbox_path)
    assert stats.seen == 9
    assert stats.inserted == 9


def test_rfc2047_headers_decoded(conn, edge_mbox_path):
    store.ingest_mbox(conn, edge_mbox_path)
    row = _edge_row(conn, "rfc2047-1@example.com")
    assert row["subject"] == "Café offre spéciale"
    assert row["from_name"] == "René"


def test_html_only_body_extracted(conn, edge_mbox_path):
    store.ingest_mbox(conn, edge_mbox_path)
    row = _edge_row(conn, "htmlonly-1@example.com")
    assert "Big sale on everything" in row["body_text"]
    assert "<" not in row["body_text"]


def test_nested_multipart(conn, edge_mbox_path):
    store.ingest_mbox(conn, edge_mbox_path)
    row = _edge_row(conn, "nested-1@example.com")
    assert "nested plain body" in row["body_text"]
    assert row["has_attachments"] == 1
    assert json.loads(row["attachment_names"]) == ["doc.pdf"]


def test_latin1_charset_decoded(conn, edge_mbox_path):
    store.ingest_mbox(conn, edge_mbox_path)
    row = _edge_row(conn, "latin1-1@example.com")
    assert "café au lait" in row["body_text"]


def test_bogus_charset_falls_back(conn, edge_mbox_path):
    store.ingest_mbox(conn, edge_mbox_path)
    row = _edge_row(conn, "bogus-charset-1@example.com")
    assert "weird charset body" in row["body_text"]


def test_missing_message_id_with_gm_msgid(conn, edge_mbox_path):
    store.ingest_mbox(conn, edge_mbox_path)
    row = conn.execute(
        "SELECT * FROM messages WHERE gmail_msgid='555000111222333444'"
    ).fetchone()
    assert row is not None
    assert row["rfc_message_id"] is None


def test_unclosed_style_text_survives(conn, edge_mbox_path):
    store.ingest_mbox(conn, edge_mbox_path)
    row = _edge_row(conn, "unclosed-style-1@example.com")
    assert "visible promo text" in row["body_text"]


def test_text_csv_attachment_counted(conn, edge_mbox_path):
    store.ingest_mbox(conn, edge_mbox_path)
    row = _edge_row(conn, "csv-1@example.com")
    assert row["has_attachments"] == 1
    assert "invoice.csv" in json.loads(row["attachment_names"])
    assert "invoice attached" in row["body_text"]
    assert "widget" not in row["body_text"]  # CSV content stays out of the body


def test_from_quoted_body_line_does_not_split(conn, edge_mbox_path):
    store.ingest_mbox(conn, edge_mbox_path)
    row = _edge_row(conn, "fromquote-1@example.com")
    assert "the archive, a quoted line" in row["body_text"]
    assert "last line" in row["body_text"]


def _identifierless(subject="Delivery Status Notification (Failure)", **overrides):
    from local_llm_email_cleaner.models import ParsedMessage

    fields = dict(
        gmail_msgid=None,
        thread_id=None,
        rfc_message_id=None,
        labels=None,
        date_utc="2019-04-01T10:00:00+00:00",
        date_epoch=1554112800,
        from_addr="mailer-daemon@x.example",
        from_name=None,
        from_domain="x.example",
        to_addr=USER_ADDR,
        to_all=USER_ADDR,
        subject=subject,
        body_text="bounce",
        has_attachments=False,
        attachment_names=[],
        size_bytes=2048,
        list_unsubscribe=False,
    )
    fields.update(overrides)
    return ParsedMessage(**fields)


def test_reingest_dedups_messages_without_any_identifier(conn):
    # No X-GM-MSGID and no Message-ID: dedup_key keeps re-ingest idempotent.
    store.insert_messages(conn, [_identifierless()])
    second = store.insert_messages(conn, [_identifierless()])
    assert second.inserted == 0
    n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE from_addr='mailer-daemon@x.example'"
    ).fetchone()[0]
    assert n == 1


def test_distinct_identifierless_messages_are_both_kept(conn):
    # Different content -> different dedup_key -> not collapsed.
    store.insert_messages(conn, [_identifierless(subject="bounce A")])
    store.insert_messages(conn, [_identifierless(subject="bounce B")])
    n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE from_addr='mailer-daemon@x.example'"
    ).fetchone()[0]
    assert n == 2


def test_unlabeled_legacy_charset_body_not_mangled(conn):
    # A text part with no declared charset and a cp1252/latin-1 byte must not be
    # silently turned into U+FFFD by defaulting to UTF-8.
    from email import message_from_bytes

    from local_llm_email_cleaner.ingest.bodies import extract_body

    raw = (
        b"From: x@y.example\nSubject: s\n"
        b"Content-Type: text/plain\nContent-Transfer-Encoding: 8bit\n"
        b"\ncaf\xe9 au lait\n"
    )
    text, _, _ = extract_body(message_from_bytes(raw))
    assert "café au lait" in text
    assert "�" not in text


# --- pure-function unit tests -------------------------------------------------


def test_html_to_text_unclosed_style_unit():
    from local_llm_email_cleaner.ingest.bodies import html_to_text

    out = html_to_text("<style>.a{}\n<p>hidden? no</p>")
    assert "hidden? no" in out
    out = html_to_text("<style>.a{}</style><p>after closed</p><script>x()")
    assert "after closed" in out
    assert ".a{}" not in out  # closed block removed wholly
