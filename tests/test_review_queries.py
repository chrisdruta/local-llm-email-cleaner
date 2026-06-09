"""Review-browser SQL builders (shared by the Streamlit app and CSV export)."""

from __future__ import annotations

import sqlite3

import pytest

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
