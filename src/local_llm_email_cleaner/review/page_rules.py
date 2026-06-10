"""Rules page: per-rule effectiveness, sample matches, and re-run.

The rules.toml file stays the source of truth — edit it in your editor, then
click "Re-run rules" here (or `email-cleaner rules --reset`). Stored LLM
verdicts survive a re-run; only newly LLM-bound rows await the next classify.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from local_llm_email_cleaner.review import queries
from local_llm_email_cleaner.review.components import (
    df_query,
    get_cfg,
    get_conn,
    load_rules,
    review_browser,
)
from local_llm_email_cleaner.rules import engine


def _rules_table(ruleset, stats: pd.DataFrame) -> pd.DataFrame:
    rows = []
    by_name = (
        {r["rule_name"]: r for _, r in stats.iterrows()} if not stats.empty else {}
    )
    for rule in ruleset.rules:
        s = by_name.get(rule.name)
        rows.append(
            {
                "rule": rule.name,
                "priority": rule.priority,
                "action": rule.action,
                "protect": rule.protect,
                "llm check": rule.confirm_with_llm,
                "ephemeral": rule.ephemeral,
                "enabled": rule.enabled,
                "hits": int(s["hits"]) if s is not None else 0,
                "wins": int(s["wins"]) if s is not None else 0,
                "pending": int(s["wins_pending"]) if s is not None else 0,
                "approved": int(s["wins_approved"]) if s is not None else 0,
                "applied": int(s["wins_applied"]) if s is not None else 0,
                "description": rule.description,
            }
        )
    return pd.DataFrame(rows)


def render() -> None:
    cfg = get_cfg()
    ruleset, errors = load_rules()
    if errors is not None:
        st.error(f"`{cfg.rules_path}` has problems — fix them and reload:")
        for err in errors.errors:
            st.markdown(f"- {err}")
        return
    assert ruleset is not None

    conn = get_conn()
    try:
        st.caption(
            f"`{cfg.rules_path}` — {len(ruleset.ordered_rules())} enabled rules "
            f"(of {len(ruleset.rules)}). Edit the file in your editor, then "
            "re-run below. Validate edits any time with "
            "`email-cleaner rules --check`."
        )

        unruled = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE ruled_at IS NULL"
        ).fetchone()[0]
        c1, c2 = st.columns(2)
        if c1.button(f"Run rules on new messages ({unruled})"):
            with st.spinner("Evaluating rules..."):
                counts = engine.run_rules(conn, ruleset, engine.load_context(conn))
            st.success(f"Evaluated: {dict(counts) or 'nothing new'}")
            st.rerun()
        if c2.button(
            "Re-run rules on all pending rows",
            help="Re-evaluates every still-pending message against the current "
            "rules.toml. Stored LLM verdicts are kept and re-finalized — "
            "no LLM time is re-paid. Approved/applied rows are untouched.",
        ):
            with st.spinner("Re-evaluating rules..."):
                counts = engine.run_rules(
                    conn, ruleset, engine.load_context(conn), reset=True
                )
            st.success(f"Re-evaluated: {dict(counts)}")
            st.rerun()

        stats = df_query(conn, queries.RULE_STATS)
        st.dataframe(
            _rules_table(ruleset, stats),
            hide_index=True,
            column_config={
                "description": st.column_config.TextColumn("description", width="large")
            },
        )

        st.divider()
        st.subheader("Inspect a rule's matches")
        pick = st.selectbox("Rule", [r.name for r in ruleset.rules], key="rules_pick")
        if pick:
            sql, params = queries.build_message_query({"rule_name": [pick]})
            review_browser(conn, df_query(conn, sql, params), key=f"rule_{pick}")
    finally:
        conn.close()
