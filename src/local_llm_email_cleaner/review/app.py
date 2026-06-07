"""Streamlit review UI.

Launched by `email-cleaner review` (which wraps `streamlit run` on this file),
or directly: `uv run streamlit run src/local_llm_email_cleaner/review/app.py`.

This app only ever writes messages.review_status / review_note — it never
touches Gmail.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

from local_llm_email_cleaner.config import load_config
from local_llm_email_cleaner.models import ProposedAction, ReviewStatus, StagedLabel
from local_llm_email_cleaner.review import queries

st.set_page_config(page_title="email-cleaner review", layout="wide")

cfg = load_config()


def get_conn() -> sqlite3.Connection:
    # A fresh connection per rerun: cheap, and avoids cross-thread reuse issues.
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def set_status(conn: sqlite3.Connection, ids: list[int], status: str) -> int:
    if not ids:
        return 0
    cur = conn.executemany(
        # Never overwrite rows the runner already applied.
        "UPDATE messages SET review_status=? WHERE id=? AND review_status != 'applied'",
        [(status, i) for i in ids],
    )
    conn.commit()
    return cur.rowcount


def df_query(
    conn: sqlite3.Connection, sql: str, params: tuple | dict = ()
) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


#: Message-table display: important columns first; short labels and sized
#: columns. Subject/reason are wide so they wrap when multiline rows are on.
MESSAGE_COLUMN_ORDER = [
    "select",
    "id",
    "date_utc",
    "from_addr",
    "subject",
    "staged_label",
    "proposed_action",
    "review_status",
    "ai_category",
    "ai_confidence",
    "ai_reason",
    "from_domain",
    "classified_by",
    "size_bytes",
    "has_attachments",
]
MESSAGE_COLUMN_CONFIG = {
    "select": st.column_config.CheckboxColumn("select", width="small", pinned=True),
    "id": st.column_config.NumberColumn("id", width="small", pinned=True),
    "date_utc": st.column_config.DatetimeColumn(
        "date", format="YYYY-MM-DD", width="small"
    ),
    "from_addr": st.column_config.TextColumn("from", width="medium"),
    "subject": st.column_config.TextColumn("subject", width="large"),
    "staged_label": st.column_config.TextColumn("staged", width="small"),
    "proposed_action": st.column_config.TextColumn("action", width="small"),
    "review_status": st.column_config.TextColumn("status", width="small"),
    "ai_category": st.column_config.TextColumn("category", width="small"),
    "ai_confidence": st.column_config.NumberColumn("conf", format="%.2f", width=60),
    "ai_reason": st.column_config.TextColumn("reason", width="large"),
    "from_domain": st.column_config.TextColumn("domain", width="small"),
    "classified_by": st.column_config.TextColumn("by", width="small"),
    "size_bytes": st.column_config.NumberColumn("size", format="compact", width=60),
    "has_attachments": st.column_config.CheckboxColumn("attach", width=60),
}


def message_table_with_actions(
    conn: sqlite3.Connection, df: pd.DataFrame, key: str
) -> None:
    """Render messages with select checkboxes + approve/reject controls."""
    st.metric(label="Total Rows", value=len(df))
    if df.empty:
        st.info("No messages match this view.")
        return

    df = df.copy()
    df.insert(0, "select", False)
    if "date_utc" in df:
        df["date_utc"] = pd.to_datetime(df["date_utc"], errors="coerce", utc=True)
    if "has_attachments" in df:
        df["has_attachments"] = df["has_attachments"].fillna(0).astype(bool)

    multiline = st.toggle("Multiline rows", value=True, key=f"ml_{key}")
    edited = st.data_editor(
        df,
        hide_index=True,
        disabled=[c for c in df.columns if c != "select"],
        column_config=MESSAGE_COLUMN_CONFIG,
        column_order=[c for c in MESSAGE_COLUMN_ORDER if c in df.columns]
        + [c for c in df.columns if c not in MESSAGE_COLUMN_ORDER],
        # Taller rows make text columns (subject, reason) wrap across lines.
        row_height=76 if multiline else None,
        key=f"editor_{key}",
        height=800,
    )
    selected_ids = edited.loc[edited["select"], "id"].astype(int).tolist()
    all_ids = df["id"].astype(int).tolist()

    c1, c2, c3, c4 = st.columns(4)
    if c1.button(f"Approve selected ({len(selected_ids)})", key=f"as_{key}"):
        set_status(conn, selected_ids, "approved")
        st.rerun()
    if c2.button(f"Reject selected ({len(selected_ids)})", key=f"rs_{key}"):
        set_status(conn, selected_ids, "rejected")
        st.rerun()
    if c3.button(f"Approve ALL shown ({len(all_ids)})", key=f"aa_{key}"):
        set_status(conn, all_ids, "approved")
        st.rerun()
    if c4.button(f"Reject ALL shown ({len(all_ids)})", key=f"ra_{key}"):
        set_status(conn, all_ids, "rejected")
        st.rerun()


def group_table_with_actions(
    conn: sqlite3.Connection, df: pd.DataFrame, group_col: str, key: str
) -> None:
    """Render sender/domain groups; bulk-act on each group's pending trash/archive."""
    if df.empty:
        st.info("Nothing to show yet — run ingest/rules/classify first.")
        return

    df = df.copy()
    df.insert(0, "select", False)
    edited = st.data_editor(
        df,
        hide_index=True,
        disabled=[c for c in df.columns if c != "select"],
        column_config={"select": st.column_config.CheckboxColumn("select")},
        key=f"editor_{key}",
        height="auto",
    )
    groups = edited.loc[edited["select"], group_col].tolist()
    st.caption(
        "Bulk actions apply to review_status='pending' messages with the given "
        "proposed action in the selected groups."
    )

    def pending_ids(action: str) -> list[int]:
        if not groups:
            return []
        placeholders = ",".join("?" for _ in groups)
        return [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM messages WHERE {group_col} IN ({placeholders}) "
                "AND proposed_action=? AND review_status='pending'",
                [*groups, action],
            )
        ]

    for action in ("trash", "archive"):
        ids = pending_ids(action)
        approve_col, reject_col = st.columns(2)
        if approve_col.button(
            f"Approve {action} in selected groups ({len(ids)})",
            key=f"ga_{action}_{key}",
        ):
            set_status(conn, ids, "approved")
            st.rerun()
        if reject_col.button(
            f"Reject {action} in selected groups ({len(ids)})", key=f"gr_{action}_{key}"
        ):
            set_status(conn, ids, "rejected")
            st.rerun()


def render_detail(conn: sqlite3.Connection) -> None:
    with st.expander("Inspect a message by id"):
        msg_id = st.number_input("Message id", min_value=1, step=1, value=1)
        if st.button("Load message"):
            row = conn.execute(queries.MESSAGE_DETAIL, (msg_id,)).fetchone()
            if row is None:
                st.warning(f"No message with id {msg_id}")
                return
            st.write({k: row[k] for k in row.keys() if k != "body_text"})
            st.text_area("Body", row["body_text"] or "", height=240)
            hits = df_query(conn, queries.RULE_HITS_FOR_MESSAGE, (msg_id,))
            if not hits.empty:
                st.write("Rule hits:", hits)
            history = df_query(conn, queries.ACTIONS_FOR_MESSAGE, (msg_id,))
            if not history.empty:
                st.write("Action history:", history)


def main() -> None:
    st.title("email-cleaner — review proposals")
    conn = get_conn()

    view = st.sidebar.radio(
        "View",
        (
            "Overview",
            "Proposed trash",
            "Auto-approved",
            "By sender",
            "By domain",
            "Largest senders",
            "Oldest promotions",
            "Uncertain classifications",
            "Search",
            "By status",
        ),
    )

    if view == "Overview":
        st.subheader("Pipeline state")
        st.write("By review status / proposed action:")
        st.dataframe(df_query(conn, queries.STATUS_COUNTS), hide_index=True)
        st.write("By staged label:")
        st.dataframe(df_query(conn, queries.STAGED_COUNTS), hide_index=True)

    elif view == "Proposed trash":
        statuses = st.sidebar.multiselect(
            "Review status",
            ["pending", "auto_approved", "approved", "rejected", "applied", "skipped"],
            default=["pending"],
        )
        if statuses:
            sql = queries.PROPOSED_TRASH.format(statuses=queries.in_clause(statuses))
            message_table_with_actions(conn, df_query(conn, sql), key="trash")

    elif view == "Auto-approved":
        st.caption(
            "Messages the policy gates auto-approved for trash or archive. Reject "
            "anything you want to keep — `apply` will act on the rest."
        )
        message_table_with_actions(
            conn, df_query(conn, queries.AUTO_APPROVED), key="auto"
        )

    elif view == "By sender":
        group_table_with_actions(
            conn, df_query(conn, queries.BY_SENDER), "from_addr", "sender"
        )

    elif view == "By domain":
        group_table_with_actions(
            conn, df_query(conn, queries.BY_DOMAIN), "from_domain", "domain"
        )

    elif view == "Largest senders":
        st.dataframe(df_query(conn, queries.LARGEST_SENDERS), hide_index=True)

    elif view == "Oldest promotions":
        message_table_with_actions(
            conn, df_query(conn, queries.OLDEST_PROMOS), key="promos"
        )

    elif view == "Uncertain classifications":
        threshold = st.sidebar.slider(
            "Confidence below", 0.0, 1.0, cfg.uncertain_confidence_threshold, 0.05
        )
        message_table_with_actions(
            conn, df_query(conn, queries.UNCERTAIN, (threshold,)), key="uncertain"
        )

    elif view == "Search":
        term = st.text_input("Full-text search (FTS5 syntax)")
        if term:
            try:
                message_table_with_actions(
                    conn, df_query(conn, queries.FTS_SEARCH, (term,)), key="search"
                )
            except (sqlite3.OperationalError, pd.errors.DatabaseError) as exc:
                st.error(f"Bad FTS query: {exc}")

    elif view == "By status":
        group_by = st.sidebar.radio(
            "Group by", ("Review status / proposed action", "Staged label")
        )
        ALL = "(all)"
        params: dict[str, str | None] = {"status": None, "action": None, "label": None}
        if group_by == "Staged label":
            label = st.sidebar.selectbox("Staged label", [ALL, *StagedLabel])
            if label != ALL:
                params["label"] = label
        else:
            status = st.sidebar.selectbox("Review status", [ALL, *ReviewStatus])
            action = st.sidebar.selectbox("Proposed action", [ALL, *ProposedAction])
            if status != ALL:
                params["status"] = status
            if action != ALL:
                params["action"] = action

        total = conn.execute(queries.BY_STATUS_COUNT, params).fetchone()[0]
        df = df_query(conn, queries.BY_STATUS, params)
        if total > len(df):
            st.caption(f"Showing newest {len(df)} of {total} matching messages.")
        # Key the editor by the active filters so checkbox state resets when
        # the filter (and therefore the row set) changes.
        filter_key = "_".join(str(v) for v in params.values())
        message_table_with_actions(conn, df, key=f"bystatus_{filter_key}")

    render_detail(conn)
    conn.close()


main()
