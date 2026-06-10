"""Policy page: tune the auto-approval gates, preview, then run them.

The preview and the gates are built from the same SQL predicates
(policy._trash_gate_where / _archive_gate_where), so what you see is exactly
what "Run gates" approves. Tuned values persist in the meta table and win
over config.toml (shown by `email-cleaner status` and the policy CLI).
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import streamlit as st

from local_llm_email_cleaner import policy
from local_llm_email_cleaner.review.components import get_cfg, get_conn


def _sample_df(rows) -> pd.DataFrame:
    return pd.DataFrame([dict(r) for r in rows])


def render() -> None:
    cfg = get_cfg()
    conn = get_conn()
    try:
        saved = policy.PolicyParams.load(conn, cfg)

        st.caption(
            "Auto-approval is the ONLY thing policy does — nothing touches "
            "Gmail until you apply. Tuning here is saved to the database and "
            "overrides config.toml."
        )

        c = st.columns(6)
        trash_conf = c[0].slider(
            "Auto-trash min LLM confidence",
            0.5,
            1.0,
            saved.auto_trash_min_confidence,
            0.01,
        )
        age_months = c[1].number_input(
            "Auto-trash min age (months)", 0, 120, saved.auto_trash_min_age_months
        )
        eph_days = c[2].number_input(
            "Ephemeral grace (days)",
            0,
            365,
            saved.auto_trash_ephemeral_min_age_days,
            help="Digests flagged ephemeral by BOTH the rule and the LLM skip "
            "the age floor and only wait this many days.",
        )
        archive_conf = c[3].slider(
            "Auto-archive min LLM confidence",
            0.5,
            1.05,
            saved.auto_archive_min_confidence,
            0.01,
            help="Set above 1.0 to disable auto-archive entirely.",
        )
        llm_only_conf = c[4].slider(
            "LLM-only min confidence",
            0.5,
            1.05,
            saved.auto_llm_only_min_confidence,
            0.01,
            help="Messages NO rule matched may auto-approve on the LLM's word "
            "alone above this (higher) bar — age floor, attachments, contact "
            "and keep-hit guards still apply. Set above 1.0 to disable.",
        )
        rule_only = c[5].checkbox(
            "Allow rule-only auto-trash",
            saved.auto_trash_allow_rule_only,
            help="Let trash rules with confirm_with_llm=false auto-approve "
            "without any LLM confidence. Voice records and other rule-only "
            "trash otherwise always need explicit approval.",
        )

        params = dataclasses.replace(
            saved,
            auto_trash_min_confidence=trash_conf,
            auto_trash_min_age_months=int(age_months),
            auto_trash_ephemeral_min_age_days=int(eph_days),
            auto_archive_min_confidence=archive_conf,
            auto_llm_only_min_confidence=llm_only_conf,
            auto_trash_allow_rule_only=rule_only,
        )

        preview = policy.preview_policy(conn, params)
        m1, m2 = st.columns(2)
        m1.metric("Would auto-approve for TRASH", preview.trash_count)
        m2.metric("Would auto-approve for ARCHIVE", preview.archive_count)

        with st.expander(
            f"Trash sample (oldest {len(preview.trash_sample)})", expanded=False
        ):
            st.dataframe(_sample_df(preview.trash_sample), hide_index=True)
        with st.expander(
            f"Archive sample (oldest {len(preview.archive_sample)})", expanded=False
        ):
            st.dataframe(_sample_df(preview.archive_sample), hide_index=True)

        if st.button("Run gates with these settings", type="primary"):
            params.save(conn)
            result = policy.apply_policy(conn, params)
            st.success(
                f"Auto-approved {result['auto_approved']} for trash and "
                f"{result['auto_archived']} for archive. "
                f"{result['pending_trash_for_review']} trash / "
                f"{result['pending_archive_for_review']} archive proposals "
                "left for human review. Settings saved."
            )
        st.caption(
            "Re-running is always safe: earlier auto-approvals are demoted "
            "first, then re-gated with the new settings. Human approve/reject "
            "decisions are never touched."
        )
    finally:
        conn.close()
