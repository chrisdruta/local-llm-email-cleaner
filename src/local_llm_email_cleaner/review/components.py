"""Shared widgets and helpers for the Streamlit review app.

Pages import from here; this module owns the config/connection plumbing, the
message grid (selection -> bulk actions + detail panel), the stored-column
decision summary, and the group bulk-action snapshot pattern.
"""

from __future__ import annotations

import json
import sqlite3

import pandas as pd
import streamlit as st

from local_llm_email_cleaner import db, export
from local_llm_email_cleaner.config import load_config
from local_llm_email_cleaner.models import ACTIONABLE_ACTIONS, ReviewStatus
from local_llm_email_cleaner.review import queries
from local_llm_email_cleaner.rules.ruleset import (
    RulesConfigError,
    RuleSet,
    load_ruleset,
)


@st.cache_resource
def get_cfg():
    return load_config()


def get_conn() -> sqlite3.Connection:
    # A fresh connection per rerun (cheap, avoids cross-thread reuse issues),
    # configured exactly like every other pipeline stage.
    return db.connect(get_cfg().db_path)


def load_rules() -> tuple[RuleSet | None, RulesConfigError | None]:
    """The current rules.toml, or its validation errors (never both)."""
    try:
        return load_ruleset(get_cfg().rules_path), None
    except RulesConfigError as exc:
        return None, exc


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
#: columns. The long llm_reason is intentionally NOT in the grid — it lives in
#: the detail panel so the table stays scannable.
MESSAGE_COLUMN_ORDER = [
    "id",
    "date_utc",
    "from_addr",
    "subject",
    "action",
    "review_status",
    "rule_name",
    "rule_action",
    "llm_action",
    "llm_confidence",
    "decision_source",
    "llm_category",
    "from_domain",
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
    "action": st.column_config.TextColumn("action", width="small"),
    "review_status": st.column_config.TextColumn("status", width="small"),
    "rule_name": st.column_config.TextColumn("rule", width="small"),
    "rule_action": st.column_config.TextColumn("rule says", width="small"),
    "llm_action": st.column_config.TextColumn("llm says", width="small"),
    "llm_confidence": st.column_config.NumberColumn("conf", format="%.2f", width=60),
    "decision_source": st.column_config.TextColumn("by", width="small"),
    "llm_category": st.column_config.TextColumn("category", width="small"),
    "from_domain": st.column_config.TextColumn("domain", width="small"),
    "size_bytes": st.column_config.NumberColumn("size", format="compact", width=60),
    "has_attachments": st.column_config.CheckboxColumn("attach", width=60),
}


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


def decision_summary(row: sqlite3.Row, hits: list[sqlite3.Row]) -> str:
    """Markdown narrative of how this message got its decision — built entirely
    from STORED verdicts (never re-evaluated), so it shows what actually
    happened, even if rules.toml has been tuned since."""
    ruleset, _ = load_rules()

    def describe(rule_name: str) -> str:
        rule = ruleset.rule(rule_name) if ruleset else None
        return f" — {rule.description}" if rule and rule.description else ""

    lines: list[str] = []

    # Rules stage
    if row["ruled_at"] is None:
        lines.append("**Rules:** not yet evaluated.")
    elif row["rule_name"] is None:
        lines.append("**Rules:** no rule matched — handed to the LLM.")
    else:
        flags = []
        if row["rule_protected"]:
            flags.append("protect")
        if row["rule_ephemeral"]:
            flags.append("ephemeral")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"**Rules:** `{row['rule_name']}` won, voting "
            f"**{row['rule_action']}**{suffix}{describe(row['rule_name'])}"
        )
        losers = [h["rule_name"] for h in hits if not h["won"]]
        if losers:
            lines.append(f"Also matched (outranked): {', '.join(losers)}.")

    # LLM stage
    if row["llm_action"] is None:
        if row["action"] is None:
            lines.append("**LLM:** awaiting classification.")
        elif row["decision_source"] == "rule":
            lines.append("**LLM:** skipped — the rule decided alone.")
    else:
        conf = row["llm_confidence"]
        conf_txt = f" ({conf:.2f})" if conf is not None else ""
        lines.append(
            f"**LLM:** said **{row['llm_action']}**{conf_txt} — "
            f"{row['llm_reason'] or 'no reason recorded'}"
        )
        if row["rule_action"] is not None:
            if row["llm_action"] == row["rule_action"]:
                lines.append("The LLM **confirmed** the rule's verdict.")
            else:
                lines.append(
                    f"The LLM **disagreed** with the rule "
                    f"({row['rule_action']} vs {row['llm_action']}) — routed to "
                    f"human review."
                )

    # Outcome
    lines.append(
        f"**Final:** {row['action'] or '(undecided)'} · decided by "
        f"{row['decision_source'] or '—'} · review status "
        f"**{row['review_status']}**"
        + (f" ({row['review_note']})" if row["review_note"] else "")
    )
    return "  \n".join(lines)


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
    """Full email detail for the selected row: headers, decision, body, audit."""
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
        f"**Attachments:** {_fmt_attachments(row)}"
    )

    hits = conn.execute(queries.RULE_HITS_FOR_MESSAGE, (msg_id,)).fetchall()
    st.markdown(decision_summary(row, hits))

    # body_text is plain text (no HTML stored); read-only scrollable box.
    st.text_area(
        "Body",
        row["body_text"] or "",
        height=400,
        disabled=True,
        key=f"body_{key}_{msg_id}",
    )

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
        "concurrent rules/classify/policy run) are never approved unseen."
    )

    def pending_ids(action: str) -> list[int]:
        if not groups:
            return []
        placeholders = ",".join("?" for _ in groups)
        return [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM messages WHERE {group_col} IN ({placeholders}) "
                "AND action=? AND review_status='pending'",
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
