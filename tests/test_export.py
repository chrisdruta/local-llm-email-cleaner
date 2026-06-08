"""CSV export: approved-only rows, formula-injection neutralization."""

from __future__ import annotations

import csv

from conftest import insert_message

from local_llm_email_cleaner.export import export_actions
from local_llm_email_cleaner.review import queries


def test_export_writes_only_approved_actionable(conn, tmp_path):
    insert_message(
        conn,
        rfc_message_id="a@example.com",
        proposed_action="trash",
        review_status="approved",
    )
    insert_message(
        conn,
        rfc_message_id="p@example.com",
        proposed_action="trash",
        review_status="pending",
    )
    insert_message(
        conn,
        rfc_message_id="k@example.com",
        proposed_action="keep",
        review_status="approved",
    )

    out = tmp_path / "actions.csv"
    n = export_actions(conn, out)
    assert n == 1
    rows = list(csv.DictReader(out.open()))
    assert [r["rfc_message_id"] for r in rows] == ["a@example.com"]


def test_export_neutralizes_formula_injection(conn, tmp_path):
    insert_message(
        conn,
        rfc_message_id="evil@example.com",
        proposed_action="trash",
        review_status="approved",
        subject='=HYPERLINK("http://evil.example","click")',
        ai_reason="@SUM(A1:A9)",
    )

    out = tmp_path / "actions.csv"
    export_actions(conn, out)
    row = next(csv.DictReader(out.open()))
    assert row["subject"].startswith("'=")
    assert row["reason"].startswith("'@")
    # Non-text columns are untouched.
    assert row["action"] == "trash"


def test_update_status_if_pending_guards_non_pending(conn):
    pending = insert_message(
        conn, rfc_message_id="p1@example.com", review_status="pending"
    )
    applied = insert_message(
        conn, rfc_message_id="d1@example.com", review_status="applied"
    )
    skipped = insert_message(
        conn, rfc_message_id="s1@example.com", review_status="skipped"
    )

    changed = queries.update_status_if_pending(
        conn, [pending, applied, skipped], "approved"
    )
    assert changed == 1

    def status(msg_id):
        return conn.execute(
            "SELECT review_status FROM messages WHERE id=?", (msg_id,)
        ).fetchone()[0]

    assert status(pending) == "approved"
    assert status(applied) == "applied"  # runner's record is never overwritten
    assert status(skipped) == "skipped"  # snapshot staleness can't resurrect rows

    assert queries.update_status_if_pending(conn, [], "approved") == 0
