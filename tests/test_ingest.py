"""Ingest: streaming parse, identifier preservation, idempotency, FTS, contacts."""

from __future__ import annotations

import json

from conftest import FRIEND_ADDR, USER_ADDR

from local_llm_email_cleaner.ingest import contacts, store


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
