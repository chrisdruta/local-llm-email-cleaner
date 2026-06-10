"""Review-UI write helpers: human decisions and the approve guard."""

from __future__ import annotations

from conftest import insert_message

from local_llm_email_cleaner.review.components import set_decision, set_status

RULED = "2026-01-01T00:00:00"


def needs_decision_row(conn, **overrides) -> int:
    """A disagreement: rule said trash, LLM said archive, nothing decided."""
    return insert_message(
        conn,
        ruled_at=RULED,
        rule_name="promotional_label",
        rule_action="trash",
        llm_action="archive",
        llm_confidence=0.8,
        **overrides,
    )


def row_of(conn, msg_id):
    return conn.execute(
        "SELECT staged_action, decision_source, review_status FROM messages WHERE id=?",
        (msg_id,),
    ).fetchone()


def test_set_decision_decides_and_approves(conn):
    mid = needs_decision_row(conn)
    assert set_decision(conn, [mid], "keep") == 1  # free choice: neither vote
    row = row_of(conn, mid)
    assert row["staged_action"] == "keep"
    assert row["decision_source"] == "human"
    assert row["review_status"] == "approved"


def test_set_decision_overrides_a_prior_human_pick(conn):
    mid = needs_decision_row(conn)
    set_decision(conn, [mid], "trash")
    set_decision(conn, [mid], "archive")  # changed my mind
    row = row_of(conn, mid)
    assert row["staged_action"] == "archive"
    assert row["decision_source"] == "human"


def test_set_decision_never_touches_applied_rows(conn):
    mid = needs_decision_row(conn, review_status="applied")
    assert set_decision(conn, [mid], "trash") == 0
    assert row_of(conn, mid)["staged_action"] is None


def test_approve_skips_undecided_rows(conn):
    undecided = needs_decision_row(conn)
    decided = insert_message(
        conn,
        ruled_at=RULED,
        rule_action="trash",
        llm_action="trash",
        staged_action="trash",
        decision_source="rule+llm",
        rfc_message_id="d@x",
    )
    changed = set_status(conn, [undecided, decided], "approved")
    assert changed == 1  # only the decided row
    assert row_of(conn, undecided)["review_status"] == "pending"
    assert row_of(conn, decided)["review_status"] == "approved"


def test_reject_still_works_on_undecided_rows(conn):
    # Rejecting = "leave it alone"; no action needed, so no guard.
    mid = needs_decision_row(conn)
    assert set_status(conn, [mid], "rejected") == 1
    assert row_of(conn, mid)["review_status"] == "rejected"
