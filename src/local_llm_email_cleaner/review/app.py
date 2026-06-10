"""Streamlit review UI.

Launched by `email-cleaner review` (which wraps `streamlit run` on this file),
or directly: `uv run streamlit run src/local_llm_email_cleaner/review/app.py`.

This app only ever writes messages.review_status / review_note — it never
touches Gmail.

Layout: three pages (Review / Senders / Overview). The Review page is a single
unified browser — every filter is combinable, and clicking a row opens the
full email body in a detail panel below the table.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3

import pandas as pd
import streamlit as st

from local_llm_email_cleaner import db, export
from local_llm_email_cleaner.config import load_config
from local_llm_email_cleaner.models import (
    ACTIONABLE_ACTIONS,
    CATEGORIES,
    ProposedAction,
    ReviewStatus,
    StagedLabel,
)
from local_llm_email_cleaner.review import queries
from local_llm_email_cleaner.review.trace import build_decision_trace

st.set_page_config(page_title="email-cleaner review", layout="wide")


@st.cache_resource
def _load_cfg():
    return load_config()


cfg = _load_cfg()


def get_conn() -> sqlite3.Connection:
    # A fresh connection per rerun (cheap, avoids cross-thread reuse issues),
    # configured exactly like every other pipeline stage.
    return db.connect(cfg.db_path)


def set_status(conn: sqlite3.Connection, ids: list[int], status: str) -> int:
    if not ids:
        return 0
    cur = conn.executemany(
        # Never overwrite rows the runner already applied.
        "UPDATE messages SET review_status=? WHERE id=? AND review_status != ?",
        [(status, i, ReviewStatus.APPLIED.value) for i in ids],
    )
    conn.commit()
    return cur.rowcount


def df_query(
    conn: sqlite3.Connection, sql: str, params: tuple | list | dict = ()
) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def sanitized_csv(df: pd.DataFrame) -> bytes:
    """CSV bytes with spreadsheet-formula injection neutralized (CWE-1236).

    Reuses export._sanitize_cell so this download matches `email-cleaner
    export`'s safety. Streamlit's built-in data_editor toolbar download cannot
    be disabled programmatically — this explicit button is the safe export.
    """
    safe = df.drop(columns=["select"], errors="ignore").apply(
        lambda col: col.map(export._sanitize_cell)
    )
    return safe.to_csv(index=False).encode("utf-8")


#: Message-grid display: important columns first; short labels and sized
#: columns. The long ai_reason is intentionally NOT in the grid — it lives in
#: the detail panel so the table stays scannable.
MESSAGE_COLUMN_ORDER = [
    "id",
    "date_utc",
    "from_addr",
    "subject",
    "staged_label",
    "proposed_action",
    "review_status",
    "ai_category",
    "ai_confidence",
    "from_domain",
    "classified_by",
    "size_bytes",
    "has_attachments",
]
MESSAGE_COLUMN_CONFIG = {
    "id": st.column_config.NumberColumn("id", width="small"),
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
    "from_domain": st.column_config.TextColumn("domain", width="small"),
    "classified_by": st.column_config.TextColumn("by", width="small"),
    "size_bytes": st.column_config.NumberColumn("size", format="compact", width=60),
    "has_attachments": st.column_config.CheckboxColumn("attach", width=60),
}

# --- Review-page filter state -------------------------------------------------

#: session_state keys for each filter widget, so presets can pre-fill them.
_FILTER_KEYS = {
    "fts": "flt_fts",
    "review_status": "flt_status",
    "proposed_action": "flt_action",
    "staged_label": "flt_label",
    "ai_category": "flt_category",
    "conf": "flt_conf",
    "date_from": "flt_date_from",
    "date_to": "flt_date_to",
    "from_addr": "flt_from",
    "from_domain": "flt_domain",
    "has_attachments": "flt_attach",
}

#: empty value each filter widget resets to (and the value a preset omits).
_FILTER_DEFAULTS: dict = {
    "fts": "",
    "review_status": [],
    "proposed_action": [],
    "staged_label": [],
    "ai_category": [],
    "conf": (0.0, 1.0),
    "date_from": None,
    "date_to": None,
    "from_addr": "",
    "from_domain": "",
    "has_attachments": False,
}

#: order key drives the server-side ORDER BY (the whole result set, not just
#: the grid's capped page). Presets pre-set it; the Sort selectbox reads/writes it.
_ORDER_KEY = "flt_order"

#: order value -> human label for the Sort selectbox. Keys must stay in the
#: allowlist understood by queries.build_message_query.
_ORDER_LABELS = {"default": "Newest first", "oldest": "Oldest first"}

#: Named presets → the widget values they apply. Each maps a subset of
#: _FILTER_KEYS; unlisted widgets are reset to their empty default.
_PRESETS: dict[str, dict] = {
    "Pending trash": {
        "proposed_action": [ProposedAction.TRASH.value],
        "review_status": [ReviewStatus.PENDING.value],
    },
    "Auto-approved": {"review_status": [ReviewStatus.AUTO_APPROVED.value]},
    "Uncertain": {"conf": (0.0, cfg.uncertain_confidence_threshold)},
    "Oldest promotions": {
        "staged_label": [
            StagedLabel.DELETE_CANDIDATE.value,
            StagedLabel.UNSUBSCRIBE_CANDIDATE.value,
        ],
        "_order": "oldest",
    },
}


def _set_filters(spec: dict) -> None:
    """Write `spec`'s values into the filter widgets' session_state, resetting
    every unlisted filter to its empty default."""
    for key, state_key in _FILTER_KEYS.items():
        st.session_state[state_key] = spec.get(key, _FILTER_DEFAULTS[key])
    st.session_state[_ORDER_KEY] = spec.get("_order", "default")


def _apply_preset(name: str) -> None:
    """Apply a named preset's values to the filter widgets (others cleared)."""
    _set_filters(_PRESETS[name])


def _clear_filters() -> None:
    _set_filters({})


def collect_filters() -> tuple[dict, str]:
    """Render the Review filter bar across the top of the page (presets, sort,
    and combinable filters); return (filters dict for the query builder, order
    key)."""
    # Presets, sort, search and confidence live in the sidebar.
    st.sidebar.subheader("Presets")
    cols = st.sidebar.columns(2)
    for i, name in enumerate(_PRESETS):
        if cols[i % 2].button(name, key=f"preset_{name}", use_container_width=True):
            _apply_preset(name)
            st.rerun()
    if st.sidebar.button("Clear filters", use_container_width=True):
        _clear_filters()
        st.rerun()

    st.sidebar.subheader("Sort")
    # Server-side sort over the whole result set — the grid's own column-header
    # sort only reorders the capped page it already holds.
    st.sidebar.selectbox(
        "Order by date",
        list(_ORDER_LABELS),
        format_func=_ORDER_LABELS.get,
        key=_ORDER_KEY,
    )

    st.sidebar.subheader("Search")
    fts = st.sidebar.text_input("Full-text search (FTS5)", key=_FILTER_KEYS["fts"])
    conf_lo, conf_hi = st.sidebar.slider(
        "Confidence range", 0.0, 1.0, (0.0, 1.0), 0.05, key=_FILTER_KEYS["conf"]
    )

    # The combinable filters sit in a single horizontal row across the top.
    # Short labels keep all nine controls on one line; multiselects/dates get a
    # little extra width, the attachments checkbox the least.
    c = st.columns([1.4, 1.4, 1.4, 1.4, 1, 1, 1.2, 1.2, 0.7])
    review_status = c[0].multiselect(
        "Status", [s.value for s in ReviewStatus], key=_FILTER_KEYS["review_status"]
    )
    proposed_action = c[1].multiselect(
        "Action", [a.value for a in ProposedAction], key=_FILTER_KEYS["proposed_action"]
    )
    staged_label = c[2].multiselect(
        "Staged", [s.value for s in StagedLabel], key=_FILTER_KEYS["staged_label"]
    )
    ai_category = c[3].multiselect(
        "Category", list(CATEGORIES), key=_FILTER_KEYS["ai_category"]
    )
    date_from = c[4].date_input(
        "From (UTC)", value=None, key=_FILTER_KEYS["date_from"]
    )
    date_to = c[5].date_input("To (UTC)", value=None, key=_FILTER_KEYS["date_to"])
    from_addr = c[6].text_input("Sender", key=_FILTER_KEYS["from_addr"])
    from_domain = c[7].text_input("Domain", key=_FILTER_KEYS["from_domain"])
    has_attachments = c[8].checkbox("Attach", key=_FILTER_KEYS["has_attachments"])

    filters: dict = {
        "fts": fts,
        "review_status": review_status,
        "proposed_action": proposed_action,
        "staged_label": staged_label,
        "ai_category": ai_category,
        "from_addr": from_addr,
        "from_domain": from_domain,
        "has_attachments": has_attachments,
    }
    # Only apply confidence bounds when the user narrowed the full 0–1 range.
    if conf_lo > 0.0:
        filters["conf_lo"] = conf_lo
    if conf_hi < 1.0:
        filters["conf_hi"] = conf_hi

    # Dates are stored as unix seconds (date_epoch). Treat each picked day as a
    # full UTC day: from = its midnight, to = inclusive through its last second.
    if date_from is not None:
        filters["date_from"] = calendar.timegm(date_from.timetuple())
    if date_to is not None:
        filters["date_to"] = calendar.timegm(date_to.timetuple()) + 86399

    order = st.session_state.get(_ORDER_KEY, "default")
    return filters, order


def _filter_hash(filters: dict, order: str) -> str:
    """Stable short hash of the active filters, so the grid's selection state
    resets when the row set changes."""
    payload = repr(sorted(filters.items())) + order
    return hashlib.md5(payload.encode()).hexdigest()[:8]


def _prep_display(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "date_utc" in df:
        df["date_utc"] = pd.to_datetime(df["date_utc"], errors="coerce", utc=True)
    if "has_attachments" in df:
        df["has_attachments"] = df["has_attachments"].fillna(0).astype(bool)
    return df


def review_browser(conn: sqlite3.Connection, df: pd.DataFrame, key: str) -> None:
    """Render the message grid with row-selection → bulk actions + detail panel.

    Selecting rows drives BOTH the bulk approve/reject controls (all selected
    rows) and the detail panel (the last-selected row's full email).
    """
    if df.empty:
        st.info("No messages match these filters.")
        return

    view_df = _prep_display(df)
    multiline = st.toggle("Multiline rows", value=True, key=f"ml_{key}")
    event = st.dataframe(
        view_df,
        hide_index=True,
        column_config=MESSAGE_COLUMN_CONFIG,
        column_order=[c for c in MESSAGE_COLUMN_ORDER if c in view_df.columns]
        + [c for c in view_df.columns if c not in MESSAGE_COLUMN_ORDER],
        on_select="rerun",
        selection_mode="multi-row",
        # Taller rows let the subject column wrap across lines.
        row_height=76 if multiline else None,
        height=560,
        key=f"grid_{key}",
    )
    # Positional indices into view_df (same object/order we passed in).
    sel = event.selection["rows"]
    selected_ids = view_df.iloc[sel]["id"].astype(int).tolist()
    all_ids = view_df["id"].astype(int).tolist()

    c1, c2, c3, c4 = st.columns(4)
    if c1.button(f"Approve selected ({len(selected_ids)})", key=f"as_{key}"):
        set_status(conn, selected_ids, ReviewStatus.APPROVED.value)
        st.rerun()
    if c2.button(f"Reject selected ({len(selected_ids)})", key=f"rs_{key}"):
        set_status(conn, selected_ids, ReviewStatus.REJECTED.value)
        st.rerun()
    if c3.button(f"Approve ALL shown ({len(all_ids)})", key=f"aa_{key}"):
        set_status(conn, all_ids, ReviewStatus.APPROVED.value)
        st.rerun()
    if c4.button(f"Reject ALL shown ({len(all_ids)})", key=f"ra_{key}"):
        set_status(conn, all_ids, ReviewStatus.REJECTED.value)
        st.rerun()

    # Export selected rows when any are selected; otherwise everything shown.
    export_df = view_df.iloc[sel] if sel else view_df
    csv_label = (
        f"Download selected CSV ({len(sel)})"
        if sel
        else f"Download CSV ({len(all_ids)})"
    )
    st.download_button(
        f"{csv_label} (sanitized)",
        data=sanitized_csv(export_df),
        file_name=f"{key}_export.csv",
        mime="text/csv",
        key=f"dl_{key}",
        help="Formula-injection-safe export. Prefer this over the table "
        "toolbar's built-in download.",
    )

    st.divider()
    active_id = selected_ids[-1] if selected_ids else None
    render_message_detail(conn, active_id, key)


def _fmt_attachments(row) -> str:
    if not row["has_attachments"]:
        return "none"
    raw = row["attachment_names"]
    if not raw:
        return "(yes, names unknown)"
    try:
        names = json.loads(raw)
        if isinstance(names, list):
            return ", ".join(str(n) for n in names) or "(yes)"
    except (ValueError, TypeError):
        pass
    return str(raw)


def render_message_detail(
    conn: sqlite3.Connection, msg_id: int | None, key: str
) -> None:
    """Full email detail for the selected row: headers, reason, body, audit."""
    if msg_id is None:
        st.caption("Select a row above to read the full email.")
        return
    row = conn.execute(queries.MESSAGE_DETAIL, (msg_id,)).fetchone()
    if row is None:
        st.warning(f"Message {msg_id} not found.")
        return

    st.subheader(row["subject"] or "(no subject)")

    a, r = st.columns(2)
    if a.button("Approve this", key=f"da_{key}_{msg_id}"):
        set_status(conn, [msg_id], ReviewStatus.APPROVED.value)
        st.rerun()
    if r.button("Reject this", key=f"dr_{key}_{msg_id}"):
        set_status(conn, [msg_id], ReviewStatus.REJECTED.value)
        st.rerun()

    st.markdown(
        f"**From:** {row['from_addr'] or ''}  \n"
        f"**To:** {row['to_addr'] or ''}  \n"
        f"**Date:** {row['date_utc'] or ''}  \n"
        f"**Gmail labels:** {row['labels'] or ''}  \n"
        f"**Attachments:** {_fmt_attachments(row)}  \n"
        f"**Status:** {row['review_status']} · **Action:** {row['proposed_action']} "
        f"· **Staged:** {row['staged_label']}  \n"
        f"**Category:** {row['ai_category']} · **Confidence:** {row['ai_confidence']} "
        f"· **By:** {row['classified_by']}"
    )
    with st.expander("AI reason", expanded=True):
        st.write(row["ai_reason"] or "—")

    # body_text is plain text (no HTML stored); read-only scrollable box.
    st.text_area(
        "Body",
        row["body_text"] or "",
        height=400,
        disabled=True,
        key=f"body_{key}_{msg_id}",
    )

    hits = df_query(conn, queries.RULE_HITS_FOR_MESSAGE, (msg_id,))
    if not hits.empty:
        st.caption("Rule hits")
        st.dataframe(hits, hide_index=True)
    with st.expander("Decision trace", expanded=False):
        st.markdown(build_decision_trace(conn, row).to_markdown())
    history = df_query(conn, queries.ACTIONS_FOR_MESSAGE, (msg_id,))
    if not history.empty:
        st.caption("Action history")
        st.dataframe(history, hide_index=True)


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
        "Bulk actions apply to the pending messages shown for the selected "
        "groups when this page rendered — rows staged afterwards (e.g. by a "
        "concurrent classify/policy run) are never approved unseen."
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

    for action in ACTIONABLE_ACTIONS:
        snap_key = f"ga_ids_{key}_{action}"
        # A button click reruns the whole script, so the click handler runs
        # one render AFTER the user saw the label. Act on the snapshot from
        # that render (prev_ids) — a fresh query here would approve rows the
        # user never saw — while the label shows this render's fresh list,
        # which becomes the snapshot the next click acts on. The still-pending
        # guard in update_status_if_pending covers rows that changed state
        # in between.
        prev_ids: list[int] = st.session_state.get(snap_key, [])
        fresh_ids = pending_ids(action)
        approve_col, reject_col = st.columns(2)
        if approve_col.button(
            f"Approve {action} in selected groups ({len(fresh_ids)})",
            key=f"ga_{action}_{key}",
        ):
            queries.update_status_if_pending(
                conn, prev_ids, ReviewStatus.APPROVED.value
            )
            st.rerun()
        if reject_col.button(
            f"Reject {action} in selected groups ({len(fresh_ids)})",
            key=f"gr_{action}_{key}",
        ):
            queries.update_status_if_pending(
                conn, prev_ids, ReviewStatus.REJECTED.value
            )
            st.rerun()
        st.session_state[snap_key] = fresh_ids


# --- Pages -------------------------------------------------------------------


def page_review(conn: sqlite3.Connection) -> None:
    filters, order = collect_filters()
    sql, params = queries.build_message_query(filters, order=order)
    count_sql, count_params = queries.build_message_count(filters)
    key = f"review_{_filter_hash(filters, order)}"
    # The count and row queries carry the user's FTS string (MATCH), so a
    # malformed search makes THEM raise — they must run inside the handler too,
    # not just review_browser, or the page crashes instead of showing the error.
    try:
        total = conn.execute(count_sql, count_params).fetchone()[0]
        df = df_query(conn, sql, params)
        if total > len(df):
            edge = "oldest" if order == "oldest" else "newest"
            st.caption(
                f"Showing {edge} {len(df)} of {total} matching messages "
                f"({queries.BROWSER_LIMIT}-row cap). Narrow the date range to "
                f"see messages beyond the cap."
            )
        else:
            st.caption(f"{total} matching messages.")
        review_browser(conn, df, key=key)
    except (sqlite3.OperationalError, pd.errors.DatabaseError) as exc:
        st.error(f"Bad query (check FTS syntax): {exc}")


def page_senders(conn: sqlite3.Connection) -> None:
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


def page_overview(conn: sqlite3.Connection) -> None:
    st.subheader("Pipeline state")
    st.write("By review status / proposed action:")
    st.dataframe(df_query(conn, queries.STATUS_COUNTS), hide_index=True)
    st.write("By staged label:")
    st.dataframe(df_query(conn, queries.STAGED_COUNTS), hide_index=True)


def main() -> None:
    st.title("email-cleaner — review proposals")
    conn = get_conn()

    page = st.sidebar.radio("Page", ("Review", "Senders", "Overview"))
    st.sidebar.divider()

    if page == "Review":
        page_review(conn)
    elif page == "Senders":
        page_senders(conn)
    else:
        page_overview(conn)

    conn.close()


main()
