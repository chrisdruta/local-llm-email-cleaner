"""Policy gate: auto-approval requires EVERY condition; re-runnable."""

from __future__ import annotations

import dataclasses

from conftest import FRIEND_ADDR, RECENT_DATE, add_rule_hit, insert_message

from local_llm_email_cleaner import policy


def eligible_row(conn, **overrides) -> int:
    """A row that passes the whole gate unless an override breaks a condition."""
    fields = dict(
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="rules+llm",
        ai_confidence=0.95,
        has_attachments=0,
    )
    fields.update(overrides)
    msg_id = insert_message(conn, **fields)
    add_rule_hit(conn, msg_id, "candidate", "promotional_label")
    return msg_id


def status_of(conn, msg_id: int) -> str:
    return conn.execute(
        "SELECT review_status FROM messages WHERE id=?", (msg_id,)
    ).fetchone()[0]


def test_gate_approves_when_all_conditions_hold(conn, cfg):
    msg_id = eligible_row(conn)
    result = policy.apply_policy(conn, cfg)
    assert result["auto_approved"] == 1
    assert status_of(conn, msg_id) == "auto_approved"


def test_gate_rejects_low_confidence(conn, cfg):
    msg_id = eligible_row(conn, ai_confidence=0.85)
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def test_gate_rejects_attachments(conn, cfg):
    msg_id = eligible_row(conn, has_attachments=1)
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def test_gate_rejects_recent_messages(conn, cfg):
    msg_id = eligible_row(
        conn,
        date_utc=RECENT_DATE.isoformat(),
        date_epoch=int(RECENT_DATE.timestamp()),
    )
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def test_gate_rejects_known_contacts(conn, cfg):
    conn.execute(
        "INSERT INTO contacts (address, domain, sent_count) VALUES (?, 'example.com', 3)",
        (FRIEND_ADDR,),
    )
    msg_id = eligible_row(conn, from_addr=FRIEND_ADDR)
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def test_gate_requires_candidate_rule_hit(conn, cfg):
    # LLM said trash, but no deterministic rule ever matched.
    msg_id = insert_message(
        conn,
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="llm",
        ai_confidence=0.99,
    )
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def test_gate_rejects_protected_messages(conn, cfg):
    msg_id = eligible_row(conn)
    add_rule_hit(conn, msg_id, "protection", "financial_legal_medical")
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def test_spam_overridden_protection_is_review_only(conn, cfg):
    # Spam-label staging records the suppressed keyword-protection hit, which
    # must keep the message out of auto-approval no matter how confident the
    # LLM is — human review only.
    msg_id = eligible_row(conn, ai_confidence=0.99)
    add_rule_hit(conn, msg_id, "candidate", "spam_label")
    add_rule_hit(conn, msg_id, "protection", "financial_legal_medical")
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def test_gate_requires_llm_confidence(conn, cfg):
    # Rules-only rows (no LLM verdict) are never auto-approved.
    msg_id = eligible_row(conn, classified_by="rules", ai_confidence=None)
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def archive_row(conn, *, rule_hit: bool = True, **overrides) -> int:
    """A rule-staged archive candidate that passes the auto-archive gate."""
    fields = dict(
        staged_label="ARCHIVE_CANDIDATE",
        proposed_action="archive",
        classified_by="rules",
        ai_confidence=None,
    )
    fields.update(overrides)
    msg_id = insert_message(conn, **fields)
    if rule_hit:
        add_rule_hit(conn, msg_id, "candidate", "receipt")
    return msg_id


def test_archive_gate_approves_rule_staged_without_llm(conn, cfg):
    # Rule-staged archive candidates never see the LLM; NULL confidence passes.
    msg_id = archive_row(conn)
    result = policy.apply_policy(conn, cfg)
    assert result["auto_archived"] == 1
    assert status_of(conn, msg_id) == "auto_approved"


def test_archive_gate_respects_llm_confidence_when_present(conn, cfg):
    low = archive_row(conn, classified_by="llm", ai_confidence=0.5)
    high = archive_row(conn, classified_by="llm", ai_confidence=0.85)
    policy.apply_policy(conn, cfg)
    assert status_of(conn, low) == "pending"
    assert status_of(conn, high) == "auto_approved"


def test_archive_gate_requires_candidate_rule_hit(conn, cfg):
    msg_id = archive_row(conn, rule_hit=False, classified_by="llm", ai_confidence=0.99)
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def test_archive_gate_rejects_protected_messages(conn, cfg):
    msg_id = archive_row(conn)
    add_rule_hit(conn, msg_id, "protection", "financial_legal_medical")
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def test_archive_gate_rejects_known_contacts(conn, cfg):
    conn.execute(
        "INSERT INTO contacts (address, domain, sent_count) VALUES (?, 'example.com', 3)",
        (FRIEND_ADDR,),
    )
    msg_id = archive_row(conn, from_addr=FRIEND_ADDR)
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "pending"


def test_archive_gate_allows_attachments(conn, cfg):
    # Unlike trash: archiving keeps the message, so attachments don't block it.
    msg_id = archive_row(conn, has_attachments=1)
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "auto_approved"


def test_archive_gate_can_be_disabled(conn, cfg):
    # A threshold > 1 blocks LLM-scored AND rules-only (NULL confidence) rows.
    llm_row = archive_row(conn, classified_by="llm", ai_confidence=0.99)
    rules_row = archive_row(conn, rfc_message_id="r2@example.com")
    disabled = dataclasses.replace(cfg, auto_archive_min_confidence=1.01)
    policy.apply_policy(conn, disabled)
    assert status_of(conn, llm_row) == "pending"
    assert status_of(conn, rules_row) == "pending"


def test_rerun_demotes_on_stricter_threshold(conn, cfg):
    msg_id = eligible_row(conn, ai_confidence=0.92)
    policy.apply_policy(conn, cfg)
    assert status_of(conn, msg_id) == "auto_approved"

    stricter = dataclasses.replace(cfg, auto_trash_min_confidence=0.99)
    policy.apply_policy(conn, stricter)
    assert status_of(conn, msg_id) == "pending"


def test_rerun_never_touches_human_decisions(conn, cfg):
    approved = eligible_row(conn)
    rejected = eligible_row(conn, rfc_message_id="r2@example.com")
    conn.execute("UPDATE messages SET review_status='approved' WHERE id=?", (approved,))
    conn.execute("UPDATE messages SET review_status='rejected' WHERE id=?", (rejected,))
    conn.commit()

    policy.apply_policy(conn, cfg)
    assert status_of(conn, approved) == "approved"
    assert status_of(conn, rejected) == "rejected"
