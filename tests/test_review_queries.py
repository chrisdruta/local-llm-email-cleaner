"""Review-browser SQL builders (shared by the Streamlit app and CSV export)."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import insert_message

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
