"""Streamlit review UI entry point.

Launched by `email-cleaner review` (which wraps `streamlit run` on this file),
or directly: `uv run streamlit run src/local_llm_email_cleaner/review/app.py`.

Pages:
  Review    — unified message browser (filters, bulk approve/reject, detail)
  Senders   — group by address/domain, bulk actions
  Rules     — per-rule hit/win stats, sample matches, re-run after toml edits
  Policy    — tune the auto-approval gates, preview, run
  Apply     — dry-run preview, then guarded live execution via the Gmail runner
  Overview  — pipeline funnel

The Review/Senders pages write only messages.review_status / review_note; the
Rules and Policy pages re-run their pipeline stages on the local DB; only the
Apply page (behind a dry-run + type-to-confirm guard) touches Gmail.
"""

from __future__ import annotations

import streamlit as st

from local_llm_email_cleaner.review import (
    page_apply,
    page_overview,
    page_policy,
    page_review,
    page_rules,
    page_senders,
)

st.set_page_config(page_title="email-cleaner review", layout="wide")

# Explicit url_path: every callable is named `render`, and st.Page infers the
# URL from the callable name, so identical names would collide.
pages = [
    st.Page(
        page_review.render,
        title="Review",
        icon=":material/inbox:",
        url_path="review",
        default=True,
    ),
    st.Page(
        page_senders.render,
        title="Senders",
        icon=":material/group:",
        url_path="senders",
    ),
    st.Page(page_rules.render, title="Rules", icon=":material/rule:", url_path="rules"),
    st.Page(
        page_policy.render, title="Policy", icon=":material/tune:", url_path="policy"
    ),
    st.Page(
        page_apply.render,
        title="Apply",
        icon=":material/rocket_launch:",
        url_path="apply",
    ),
    st.Page(
        page_overview.render,
        title="Overview",
        icon=":material/monitoring:",
        url_path="overview",
    ),
]

st.navigation(pages).run()
