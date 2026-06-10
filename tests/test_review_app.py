"""Every Streamlit page executes without an exception against a seeded DB.

Uses streamlit.testing.v1.AppTest, which runs the page scripts in-process —
it catches page-level crashes (bad imports, broken queries, st.navigation
misconfiguration) that an HTTP probe of the SPA shell cannot see.
"""

from __future__ import annotations

import textwrap

import pytest
from conftest import insert_message
from streamlit.testing.v1 import AppTest

from local_llm_email_cleaner.rules.ruleset import write_default_rules

APP_PATH = "src/local_llm_email_cleaner/review/app.py"
RULED = "2026-01-01T00:00:00"


@pytest.fixture
def ui_env(tmp_path, monkeypatch, cfg, conn, request):
    """Point the app at a seeded throwaway DB + a real rules.toml."""
    # One row in each interesting state.
    insert_message(conn)  # unruled
    insert_message(conn, ruled_at=RULED, rfc_message_id="norul@x")  # awaiting LLM
    insert_message(
        conn,
        ruled_at=RULED,
        rule_name="promotional_label",
        rule_action="trash",
        llm_action="trash",
        llm_confidence=0.95,
        llm_reason="promo",
        action="trash",
        decision_source="rule+llm",
        rfc_message_id="trash@x",
    )
    insert_message(
        conn,
        ruled_at=RULED,
        rule_name="financial_legal_medical",
        rule_action="keep",
        llm_action="trash",
        llm_confidence=0.7,
        llm_reason="dispute",
        action="review",
        decision_source="rule+llm",
        review_status="pending",
        rfc_message_id="disagree@x",
    )

    repo_app = (request.config.rootpath / APP_PATH).resolve()
    monkeypatch.chdir(tmp_path)
    write_default_rules(tmp_path / "rules.toml")
    monkeypatch.setenv("EMAIL_CLEANER_DB", str(cfg.db_path))

    # get_cfg is cached per-process; reset it so this test's env applies.
    from local_llm_email_cleaner.review import components

    components.get_cfg.clear()
    yield repo_app
    components.get_cfg.clear()


def test_app_entry_runs_default_page(ui_env):
    at = AppTest.from_file(str(ui_env), default_timeout=30)
    at.run()
    assert not at.exception, at.exception


@pytest.mark.parametrize(
    "module",
    [
        "page_review",
        "page_senders",
        "page_rules",
        "page_policy",
        "page_apply",
        "page_overview",
    ],
)
def test_each_page_renders(ui_env, module):
    at = AppTest.from_string(
        textwrap.dedent(
            f"""
            from local_llm_email_cleaner.review import {module}
            {module}.render()
            """
        ),
        default_timeout=30,
    )
    at.run()
    assert not at.exception, at.exception
