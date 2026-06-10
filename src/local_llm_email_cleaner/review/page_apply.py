"""Apply page: dry-run preview, then guarded live execution.

Same runner as `email-cleaner apply`: every action is reconciled against live
Gmail first (rfc822msgid search + metadata confirmation), every attempt is
audit-logged, trash never permanently deletes. Execution here requires a
dry-run THIS session plus typing APPLY — and a token already cached by
`email-cleaner auth` (the OAuth flow itself never runs inside this page).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from local_llm_email_cleaner.models import (
    ACTIONABLE_ACTIONS,
    APPROVABLE_STATUSES,
    sql_in_list,
)
from local_llm_email_cleaner.review.components import df_query, get_cfg, get_conn

_DRY_RUN_FLAG = "apply_dry_run_done"

_APPROVED_SUMMARY = f"""
SELECT staged_action, review_status, COUNT(*) AS n
FROM messages
WHERE review_status IN ({sql_in_list(APPROVABLE_STATUSES)})
  AND staged_action IN ({sql_in_list(ACTIONABLE_ACTIONS)})
GROUP BY staged_action, review_status ORDER BY n DESC
"""


def _run(execute: bool):
    """Run the action runner inside a status box; returns its ApplyStats."""
    from local_llm_email_cleaner.gmail import auth as gmail_auth
    from local_llm_email_cleaner.gmail import runner

    cfg = get_cfg()
    conn = get_conn()
    label = "Executing against live Gmail" if execute else "Dry run (reconcile only)"
    try:
        service = gmail_auth.get_service(cfg)
        with st.status(label, expanded=True) as status:
            line = st.empty()

            def progress(s: runner.ApplyStats) -> None:
                line.write(
                    f"{s.examined} examined · {s.succeeded} ok · "
                    f"{s.skipped} skipped · {s.errors} errors"
                )

            stats = runner.apply_actions(
                conn, cfg, service, execute=execute, progress=progress
            )
            status.update(label=f"{label} — done", state="complete")
        return stats
    finally:
        conn.close()


def _show_stats(stats, execute: bool) -> None:
    c = st.columns(4)
    c[0].metric("Examined", stats.examined)
    c[1].metric("Succeeded" if execute else "Would succeed", stats.succeeded)
    c[2].metric("Skipped", stats.skipped)
    c[3].metric("Errors", stats.errors)
    if stats.skip_reasons:
        st.caption("Skip reasons")
        st.dataframe(
            pd.DataFrame(
                sorted(stats.skip_reasons.items(), key=lambda kv: -kv[1]),
                columns=["reason", "count"],
            ),
            hide_index=True,
        )


def render() -> None:
    cfg = get_cfg()
    conn = get_conn()
    try:
        summary = df_query(conn, _APPROVED_SUMMARY)
    finally:
        conn.close()

    st.subheader("Approved actions waiting to be applied")
    if summary.empty:
        st.info(
            "Nothing approved yet. Approve messages on the Review/Senders "
            "pages or run the policy gates first."
        )
        return
    st.dataframe(summary, hide_index=True)

    if not cfg.token_path.is_file():
        st.error(
            f"No Gmail token at `{cfg.token_path}`. Run `email-cleaner auth` "
            "in a terminal first — the OAuth flow never runs inside this page."
        )
        return

    st.divider()
    st.subheader("1 · Dry run")
    st.caption(
        "Reconciles every approved action against live Gmail (read-only) and "
        "records what WOULD happen in the actions audit table. Nothing is "
        "modified."
    )
    if st.button("Dry-run all approved actions"):
        stats = _run(execute=False)
        st.session_state[_DRY_RUN_FLAG] = True
        _show_stats(stats, execute=False)

    st.divider()
    st.subheader("2 · Execute")
    dry_done = st.session_state.get(_DRY_RUN_FLAG, False)
    if not dry_done:
        st.caption("Run the dry run above first — execution unlocks afterwards.")
    confirm = st.text_input(
        "Type APPLY to enable execution",
        disabled=not dry_done,
        key="apply_confirm",
    )
    if st.button(
        "Execute against live Gmail (trash / archive)",
        type="primary",
        disabled=not (dry_done and confirm == "APPLY"),
    ):
        stats = _run(execute=True)
        st.session_state[_DRY_RUN_FLAG] = False
        _show_stats(stats, execute=True)
        st.success(
            "Done. Trashed mail sits in Gmail's Trash (auto-purged after ~30 "
            "days); archived mail keeps the "
            f"`{cfg.archive_label or '(no label configured)'}` label for easy "
            "bulk-undo."
        )
    st.caption(
        "Interrupting mid-run is safe: every mutation writes an intent row "
        "first, so a re-run reconciles and continues. Messages are only ever "
        "trashed, never permanently deleted."
    )
