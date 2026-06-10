"""Decision-trace narrative: rules hierarchy + LLM second-opinion story.

The rules story is produced by re-running the engine on the row, so each test
sets message fields that genuinely trigger the intended rule, plus the stored
ai_*/classified_by/staged_label the LLM stage would have written.
"""

from __future__ import annotations

import sqlite3

from conftest import insert_message

from local_llm_email_cleaner.review.trace import build_decision_trace


def trace_for(conn: sqlite3.Connection, msg_id: int):
    row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    return build_decision_trace(conn, row)


def test_keyword_keep_downgraded_by_llm(conn):
    # Subject trips the financial keyword protection (rules -> KEEP); the LLM
    # second opinion pulled it down to a delete candidate for review.
    msg_id = insert_message(
        conn,
        from_addr="deals@wayfair.example",
        from_domain="wayfair.example",
        subject="Your tax-free weekend deals",
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="rules+llm",
        ai_category="promotion",
        ai_confidence=0.97,
        ai_reason="old clearance promo",
    )
    t = trace_for(conn, msg_id)

    assert t.rules_label == "KEEP"
    assert any("Overridable protection" in line for line in t.rules_lines)
    assert any("financial_legal_medical" in line for line in t.rules_lines)
    # LLM moved KEEP -> DELETE_CANDIDATE, and the protection caveat is noted.
    assert any("KEEP" in line and "DELETE_CANDIDATE" in line for line in t.llm_lines)
    assert any("Protection hit retained" in line for line in t.llm_lines)
    assert any("0.97" in line for line in t.llm_lines)
    assert "DELETE_CANDIDATE" in t.to_markdown()


def test_rules_only_promo_not_sent_to_llm(conn):
    msg_id = insert_message(
        conn,
        from_addr="deals@shop.example",
        from_domain="shop.example",
        subject="Big summer sale",
        labels="Category Promotions",
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="rules",
    )
    t = trace_for(conn, msg_id)

    assert t.rules_label == "DELETE_CANDIDATE"
    assert any("promotional_label" in line for line in t.rules_lines)
    assert t.llm_lines == ["Decided by rules; not sent to the LLM."]


def test_needs_review_then_llm_primary(conn):
    # No rule matches -> NEEDS_REVIEW; the LLM is the sole classifier.
    msg_id = insert_message(
        conn,
        from_addr="someone@unknown.example",
        from_domain="unknown.example",
        subject="photos from the trip",
        body_text="here they are",
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="llm",
        ai_category="promotion",
        ai_confidence=0.95,
        ai_reason="looks like junk",
    )
    t = trace_for(conn, msg_id)

    assert t.rules_label == "NEEDS_REVIEW"
    assert any("No rule matched" in line for line in t.rules_lines)
    assert any("sole" not in line for line in t.llm_lines)  # sanity
    assert any("the LLM classified it" in line for line in t.llm_lines)
    assert any("looks like junk" in line for line in t.llm_lines)


def test_llm_disagrees_demotes_to_review(conn):
    # Rules staged a promo for delete; the LLM disagreed -> back to review.
    msg_id = insert_message(
        conn,
        from_addr="deals@shop.example",
        from_domain="shop.example",
        subject="Big summer sale",
        labels="Category Promotions",
        staged_label="NEEDS_REVIEW",
        proposed_action="review",
        classified_by="rules+llm",
        ai_category="promotion",
        ai_confidence=0.4,
        ai_reason="unsure",
    )
    t = trace_for(conn, msg_id)

    assert t.rules_label == "DELETE_CANDIDATE"
    assert any("disagreed" in line for line in t.llm_lines)


def test_voice_record_skips_llm(conn):
    msg_id = insert_message(
        conn,
        from_addr="+12164969651@unknown.email",
        from_domain="unknown.email",
        subject="SMS with Michael",
        labels="SMS",
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="voice",
    )
    t = trace_for(conn, msg_id)

    assert t.rules_label == "DELETE_CANDIDATE"
    assert any("voice" in line for line in t.rules_lines)
    assert any("intentionally skips" in line for line in t.llm_lines)
