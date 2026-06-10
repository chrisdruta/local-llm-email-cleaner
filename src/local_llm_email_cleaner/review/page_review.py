"""Review page: the unified message browser with combinable filters."""

from __future__ import annotations

import calendar
import hashlib
import sqlite3

import pandas as pd
import streamlit as st

from local_llm_email_cleaner.models import Action, DecisionSource, ReviewStatus
from local_llm_email_cleaner.review import queries
from local_llm_email_cleaner.review.components import (
    df_query,
    get_cfg,
    get_conn,
    review_browser,
)

#: session_state keys for each filter widget, so presets can pre-fill them.
_FILTER_KEYS = {
    "fts": "flt_fts",
    "review_status": "flt_status",
    "action": "flt_action",
    "decision_source": "flt_source",
    "llm_category": "flt_category",
    "conf": "flt_conf",
    "date_from": "flt_date_from",
    "date_to": "flt_date_to",
    "from_addr": "flt_from",
    "from_domain": "flt_domain",
    "has_attachments": "flt_attach",
    "no_rule": "flt_no_rule",
    "disagreement": "flt_disagreement",
}

#: empty value each filter widget resets to (and the value a preset omits).
_FILTER_DEFAULTS: dict = {
    "fts": "",
    "review_status": [],
    "action": [],
    "decision_source": [],
    "llm_category": [],
    "conf": (0.0, 1.0),
    "date_from": None,
    "date_to": None,
    "from_addr": "",
    "from_domain": "",
    "has_attachments": False,
    "no_rule": False,
    "disagreement": False,
}

#: order key drives the server-side ORDER BY (the whole result set, not just
#: the grid's capped page). Presets pre-set it; the Sort selectbox reads/writes it.
_ORDER_KEY = "flt_order"
_ORDER_LABELS = {"default": "Newest first", "oldest": "Oldest first"}


def _presets() -> dict[str, dict]:
    cfg = get_cfg()
    return {
        "Needs attention": {
            "review_status": [ReviewStatus.PENDING.value],
            "action": [Action.REVIEW.value],
        },
        "Disagreements": {"disagreement": True},
        "LLM uncertain": {"conf": (0.0, cfg.uncertain_confidence_threshold)},
        "No rule matched": {"no_rule": True},
        "Pending trash": {
            "action": [Action.TRASH.value],
            "review_status": [ReviewStatus.PENDING.value],
        },
        "Auto-approved": {"review_status": [ReviewStatus.AUTO_APPROVED.value]},
        "Oldest trash": {"action": [Action.TRASH.value], "_order": "oldest"},
    }


def _set_filters(spec: dict) -> None:
    """Write `spec`'s values into the filter widgets' session_state, resetting
    every unlisted filter to its empty default."""
    for key, state_key in _FILTER_KEYS.items():
        st.session_state[state_key] = spec.get(key, _FILTER_DEFAULTS[key])
    st.session_state[_ORDER_KEY] = spec.get("_order", "default")


def collect_filters() -> tuple[dict, str]:
    """Render the filter bar (presets, sort, combinable filters); return
    (filters dict for the query builder, order key)."""
    presets = _presets()
    st.sidebar.subheader("Presets")
    cols = st.sidebar.columns(2)
    for i, name in enumerate(presets):
        if cols[i % 2].button(name, key=f"preset_{name}", use_container_width=True):
            _set_filters(presets[name])
            st.rerun()
    if st.sidebar.button("Clear filters", use_container_width=True):
        _set_filters({})
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
        "LLM confidence range", 0.0, 1.0, (0.0, 1.0), 0.05, key=_FILTER_KEYS["conf"]
    )
    no_rule = st.sidebar.checkbox(
        "No rule matched",
        key=_FILTER_KEYS["no_rule"],
        help="Ruled, but nothing in rules.toml matched — the LLM decided alone.",
    )
    disagreement = st.sidebar.checkbox(
        "Rule/LLM disagreements",
        key=_FILTER_KEYS["disagreement"],
        help="The LLM contradicted the rule's verdict — prime rule-tuning input.",
    )

    # The combinable filters sit in a single horizontal row across the top.
    c = st.columns([1.4, 1.4, 1.4, 1.4, 1, 1, 1.2, 1.2, 0.7])
    review_status = c[0].multiselect(
        "Status", [s.value for s in ReviewStatus], key=_FILTER_KEYS["review_status"]
    )
    action = c[1].multiselect(
        "Action", [a.value for a in Action], key=_FILTER_KEYS["action"]
    )
    decision_source = c[2].multiselect(
        "Decided by",
        [s.value for s in DecisionSource],
        key=_FILTER_KEYS["decision_source"],
    )
    llm_category = c[3].multiselect(
        "Category",
        _distinct_categories(),
        key=_FILTER_KEYS["llm_category"],
    )
    date_from = c[4].date_input("From (UTC)", value=None, key=_FILTER_KEYS["date_from"])
    date_to = c[5].date_input("To (UTC)", value=None, key=_FILTER_KEYS["date_to"])
    from_addr = c[6].text_input("Sender", key=_FILTER_KEYS["from_addr"])
    from_domain = c[7].text_input("Domain", key=_FILTER_KEYS["from_domain"])
    has_attachments = c[8].checkbox("Attach", key=_FILTER_KEYS["has_attachments"])

    filters: dict = {
        "fts": fts,
        "review_status": review_status,
        "action": action,
        "decision_source": decision_source,
        "llm_category": llm_category,
        "from_addr": from_addr,
        "from_domain": from_domain,
        "has_attachments": has_attachments,
        "no_rule": no_rule,
        "disagreement": disagreement,
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


@st.cache_data(ttl=60)
def _distinct_categories() -> list[str]:
    conn = get_conn()
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT llm_category FROM messages "
                "WHERE llm_category IS NOT NULL ORDER BY 1"
            )
        ]
    finally:
        conn.close()


def _filter_hash(filters: dict, order: str) -> str:
    """Stable short hash of the active filters, so the grid's selection state
    resets when the row set changes."""
    payload = repr(sorted(filters.items())) + order
    return hashlib.md5(payload.encode()).hexdigest()[:8]


def render() -> None:
    conn = get_conn()
    try:
        filters, order = collect_filters()
        sql, params = queries.build_message_query(filters, order=order)
        count_sql, count_params = queries.build_message_count(filters)
        key = f"review_{_filter_hash(filters, order)}"
        # The count and row queries carry the user's FTS string (MATCH), so a
        # malformed search makes THEM raise — they must run inside the handler
        # too, or the page crashes instead of showing the error.
        try:
            total = conn.execute(count_sql, count_params).fetchone()[0]
            df = df_query(conn, sql, params)
            if total > len(df):
                edge = "oldest" if order == "oldest" else "newest"
                st.caption(
                    f"Showing {edge} {len(df)} of {total} matching messages "
                    f"({queries.BROWSER_LIMIT}-row cap). Narrow the date range "
                    f"to see messages beyond the cap."
                )
            else:
                st.caption(f"{total} matching messages.")
            review_browser(conn, df, key=key)
        except (sqlite3.OperationalError, pd.errors.DatabaseError) as exc:
            st.error(f"Bad query (check FTS syntax): {exc}")
    finally:
        conn.close()
