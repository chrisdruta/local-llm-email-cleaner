"""Review-browser SQL builders (shared by the Streamlit app and CSV export)."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import add_rule_hit, insert_message

from local_llm_email_cleaner.review import queries


def test_malformed_fts_query_raises_on_the_count(conn):
    """A bad FTS5 search makes the COUNT query (which carries the MATCH) raise,
    so the review page must run it inside its try/except — this guards that the
    count is the statement that needs protecting (regression for the crash)."""
    count_sql, params = queries.build_message_count({"fts": '"unbalanced'})
    assert "MATCH" in count_sql
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(count_sql, params).fetchone()


def test_well_formed_fts_query_runs(conn):
    count_sql, params = queries.build_message_count({"fts": "sale"})
    assert conn.execute(count_sql, params).fetchone()[0] == 0  # empty db, no error


def test_date_range_filters_whole_dataset(conn):
    """date_from/date_to bound the result set server-side on date_epoch, so the
    grid's page cap can't hide matching rows outside the window."""

    insert_message(conn, date_epoch=1_000)  # very old
    mid = insert_message(conn, date_epoch=2_000)
    insert_message(conn, date_epoch=3_000)  # newer

    sql, params = queries.build_message_query({"date_from": 1_500, "date_to": 2_500})
    rows = conn.execute(sql, params).fetchall()
    assert [r["id"] for r in rows] == [mid]

    # Each bound is usable on its own.
    count_sql, count_params = queries.build_message_count({"date_from": 1_500})
    assert conn.execute(count_sql, count_params).fetchone()[0] == 2


def test_order_oldest_first(conn):
    """The 'oldest' order is a server-side ORDER BY, so it reorders the whole
    set (within the cap), not just the page the grid received."""

    a = insert_message(conn, date_epoch=3_000)
    b = insert_message(conn, date_epoch=1_000)
    c = insert_message(conn, date_epoch=2_000)

    sql, params = queries.build_message_query({}, order="oldest")
    assert [r["id"] for r in conn.execute(sql, params)] == [b, c, a]

    sql, params = queries.build_message_query({}, order="default")
    assert [r["id"] for r in conn.execute(sql, params)] == [a, c, b]


RULED = "2026-01-01T00:00:00"


def test_disagreement_filter_and_query(conn):
    agree = insert_message(  # noqa: F841 — must NOT appear in results
        conn,
        ruled_at=RULED,
        rule_action="trash",
        llm_action="trash",
        action="trash",
    )
    disagree = insert_message(
        conn,
        ruled_at=RULED,
        rule_action="trash",
        llm_action="keep",
        action="review",
        rfc_message_id="d@x",
    )
    llm_only = insert_message(  # noqa: F841 — no rule verdict, not a disagreement
        conn, ruled_at=RULED, llm_action="keep", action="keep", rfc_message_id="l@x"
    )

    sql, params = queries.build_message_query({"disagreement": True})
    assert [r["id"] for r in conn.execute(sql, params)] == [disagree]
    assert [r["id"] for r in conn.execute(queries.DISAGREEMENTS)] == [disagree]


def test_no_rule_filter_excludes_unruled_rows(conn):
    unruled = insert_message(conn)  # noqa: F841 — rules haven't run
    no_match = insert_message(conn, ruled_at=RULED, rfc_message_id="n@x")
    matched = insert_message(  # noqa: F841
        conn, ruled_at=RULED, rule_name="receipt", rfc_message_id="m@x"
    )
    sql, params = queries.build_message_query({"no_rule": True})
    assert [r["id"] for r in conn.execute(sql, params)] == [no_match]


def test_confidence_filter_requires_llm_verdict(conn):
    rule_only = insert_message(conn, ruled_at=RULED, rule_action="trash")  # noqa: F841
    scored = insert_message(
        conn, ruled_at=RULED, llm_confidence=0.6, rfc_message_id="s@x"
    )
    sql, params = queries.build_message_query({"conf_lo": 0.0, "conf_hi": 0.7})
    assert [r["id"] for r in conn.execute(sql, params)] == [scored]


def test_rule_stats_counts_hits_wins_and_lifecycle(conn):
    won = insert_message(conn, ruled_at=RULED, rule_name="digest")
    add_rule_hit(conn, won, "trash", "digest", won=True)
    add_rule_hit(conn, won, "archive", "updates_label", won=False)
    other = insert_message(
        conn,
        ruled_at=RULED,
        rule_name="digest",
        review_status="auto_approved",
        rfc_message_id="o@x",
    )
    add_rule_hit(conn, other, "trash", "digest", won=True)

    stats = {r["rule_name"]: r for r in conn.execute(queries.RULE_STATS)}
    assert stats["digest"]["hits"] == 2
    assert stats["digest"]["wins"] == 2
    assert stats["digest"]["wins_pending"] == 1
    assert stats["digest"]["wins_approved"] == 1
    assert stats["updates_label"]["hits"] == 1
    assert stats["updates_label"]["wins"] == 0
