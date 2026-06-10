"""Senders page: group by address/domain with bulk approve/reject."""

from __future__ import annotations

import streamlit as st

from local_llm_email_cleaner.review import queries
from local_llm_email_cleaner.review.components import (
    df_query,
    get_conn,
    group_table_with_actions,
)


def render() -> None:
    conn = get_conn()
    try:
        sub = st.sidebar.radio("Group", ("By sender", "By domain", "Largest senders"))
        if sub == "By sender":
            group_table_with_actions(
                conn, df_query(conn, queries.BY_SENDER), "from_addr", "sender"
            )
        elif sub == "By domain":
            group_table_with_actions(
                conn, df_query(conn, queries.BY_DOMAIN), "from_domain", "domain"
            )
        else:
            st.dataframe(df_query(conn, queries.LARGEST_SENDERS), hide_index=True)
    finally:
        conn.close()
