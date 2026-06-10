"""Overview page: the pipeline funnel at a glance."""

from __future__ import annotations

import streamlit as st

from local_llm_email_cleaner.models import AWAITING_LLM_WHERE, NEEDS_DECISION_WHERE
from local_llm_email_cleaner.review import queries
from local_llm_email_cleaner.review.components import df_query, get_conn


def render() -> None:
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        unruled = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE ruled_at IS NULL"
        ).fetchone()[0]
        awaiting_llm = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {AWAITING_LLM_WHERE}"
        ).fetchone()[0]
        needs_decision = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {NEEDS_DECISION_WHERE}"
        ).fetchone()[0]
        decided = total - unruled - awaiting_llm - needs_decision
        n_contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        n_actions = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]

        c = st.columns(6)
        c[0].metric("Messages", total)
        c[1].metric("Not yet ruled", unruled)
        c[2].metric("Awaiting LLM", awaiting_llm)
        c[3].metric("Awaiting your decision", needs_decision)
        c[4].metric("Decided", decided)
        c[5].metric("Known contacts", n_contacts)

        st.subheader("Final action × decided by")
        st.dataframe(df_query(conn, queries.DECISION_COUNTS), hide_index=True)

        st.subheader("Review status × action")
        st.dataframe(df_query(conn, queries.STATUS_COUNTS), hide_index=True)

        st.caption(f"Gmail action audit rows: {n_actions}")
    finally:
        conn.close()
