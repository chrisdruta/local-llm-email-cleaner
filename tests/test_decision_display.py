"""The detail panel's decision summary is built from STORED columns only."""

from __future__ import annotations

from conftest import insert_message

from local_llm_email_cleaner.review.components import decision_summary
from local_llm_email_cleaner.review.queries import (
    MESSAGE_DETAIL,
    RULE_HITS_FOR_MESSAGE,
)

RULED = "2026-01-01T00:00:00"


def summary_for(conn, msg_id: int) -> str:
    row = conn.execute(MESSAGE_DETAIL, (msg_id,)).fetchone()
    hits = conn.execute(RULE_HITS_FOR_MESSAGE, (msg_id,)).fetchall()
    return decision_summary(row, hits)


def test_unruled_row(conn):
    mid = insert_message(conn)
    text = summary_for(conn, mid)
    assert "not yet evaluated" in text


def test_no_rule_awaiting_llm(conn):
    mid = insert_message(conn, ruled_at=RULED)
    text = summary_for(conn, mid)
    assert "no rule matched" in text
    assert "awaiting classification" in text


def test_rule_decided_alone(conn):
    mid = insert_message(
        conn,
        ruled_at=RULED,
        rule_name="voice",
        rule_action="trash",
        action="trash",
        decision_source="rule",
    )
    text = summary_for(conn, mid)
    assert "`voice` won" in text and "**trash**" in text
    assert "skipped — the rule decided alone" in text
    assert "decided by rule" in text


def test_llm_confirmation_with_losing_hits(conn):
    mid = insert_message(
        conn,
        ruled_at=RULED,
        rule_name="digest",
        rule_action="trash",
        rule_ephemeral=1,
        llm_action="trash",
        llm_confidence=0.93,
        llm_reason="stale roundup",
        action="trash",
        decision_source="rule+llm",
    )
    conn.execute(
        "INSERT INTO rule_hits (message_id, rule_name, action, won) VALUES "
        "(?, 'digest', 'trash', 1), (?, 'updates_label', 'archive', 0)",
        (mid, mid),
    )
    conn.commit()
    text = summary_for(conn, mid)
    assert "`digest` won" in text and "[ephemeral]" in text
    assert "outranked): updates_label" in text
    assert "**confirmed**" in text and "0.93" in text and "stale roundup" in text


def test_disagreement_routes_to_review(conn):
    mid = insert_message(
        conn,
        ruled_at=RULED,
        rule_name="promotional_label",
        rule_action="trash",
        llm_action="keep",
        llm_confidence=0.7,
        llm_reason="looks personal",
        action="review",
        decision_source="rule+llm",
    )
    text = summary_for(conn, mid)
    assert "**disagreed**" in text
    assert "trash vs keep" in text
    assert "Final:** review" in text
