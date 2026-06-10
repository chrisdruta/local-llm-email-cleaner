"""Policy gates: auto-approval requires EVERY condition; re-runnable;
preview == apply."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from conftest import FRIEND_ADDR, RECENT_DATE, add_rule_hit, insert_message

from local_llm_email_cleaner import policy

RULED = "2026-01-01T00:00:00"


def params(cfg, **overrides) -> policy.PolicyParams:
    return dataclasses.replace(policy.PolicyParams.from_config(cfg), **overrides)


def eligible_row(conn, **overrides) -> int:
    """A row that passes the whole trash gate unless an override breaks it:
    rule-staged trash, confirmed by the LLM, old, clean, unknown sender."""
    fields = dict(
        ruled_at=RULED,
        rule_name="promotional_label",
        rule_action="trash",
        llm_action="trash",
        llm_confidence=0.95,
        staged_action="trash",
        decision_source="rule+llm",
        has_attachments=0,
    )
    fields.update(overrides)
    msg_id = insert_message(conn, **fields)
    add_rule_hit(conn, msg_id, "trash", "promotional_label", won=True)
    return msg_id


def status_of(conn, msg_id: int) -> str:
    return conn.execute(
        "SELECT review_status FROM messages WHERE id=?", (msg_id,)
    ).fetchone()[0]


# --- trash gate ------------------------------------------------------------------


def test_gate_approves_when_all_conditions_hold(conn, cfg):
    msg_id = eligible_row(conn)
    result = policy.apply_policy(conn, params(cfg))
    assert result["auto_approved"] == 1
    assert status_of(conn, msg_id) == "auto_approved"


def test_gate_rejects_low_confidence(conn, cfg):
    msg_id = eligible_row(conn, llm_confidence=0.85)
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_gate_rejects_attachments(conn, cfg):
    msg_id = eligible_row(conn, has_attachments=1)
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_gate_rejects_recent_messages(conn, cfg):
    msg_id = eligible_row(
        conn,
        date_utc=RECENT_DATE.isoformat(),
        date_epoch=int(RECENT_DATE.timestamp()),
    )
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_gate_rejects_known_contacts(conn, cfg):
    conn.execute(
        "INSERT INTO contacts (address, domain, sent_count) VALUES (?, 'example.com', 3)",
        (FRIEND_ADDR,),
    )
    msg_id = eligible_row(conn, from_addr=FRIEND_ADDR)
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def llm_only_row(conn, action="trash", confidence=0.99, **overrides) -> int:
    """No rule matched; the LLM decided alone."""
    return insert_message(
        conn,
        ruled_at=RULED,
        llm_action=action,
        llm_confidence=confidence,
        staged_action=action,
        decision_source="llm",
        **overrides,
    )


def test_llm_only_trash_auto_approves_above_the_higher_bar(conn, cfg):
    msg_id = llm_only_row(conn, confidence=0.96)
    policy.apply_policy(conn, params(cfg))  # default bar 0.95
    assert status_of(conn, msg_id) == "auto_approved"


def test_llm_only_trash_below_the_bar_stays_pending(conn, cfg):
    # 0.92 clears the rule+llm threshold (0.90) but not the llm-only bar.
    msg_id = llm_only_row(conn, confidence=0.92)
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_llm_only_path_can_be_disabled(conn, cfg):
    msg_id = llm_only_row(conn, confidence=0.99)
    policy.apply_policy(conn, params(cfg, auto_llm_only_min_confidence=1.01))
    assert status_of(conn, msg_id) == "pending"


def test_llm_only_trash_still_needs_the_structural_guards(conn, cfg):
    recent = llm_only_row(
        conn,
        confidence=0.99,
        date_utc=RECENT_DATE.isoformat(),
        date_epoch=int(RECENT_DATE.timestamp()),
    )
    attached = llm_only_row(
        conn, confidence=0.99, has_attachments=1, rfc_message_id="att@x"
    )
    kept = llm_only_row(conn, confidence=0.99, rfc_message_id="keep@x")
    add_rule_hit(conn, kept, "keep", "financial_legal_medical")
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, recent) == "pending"  # age floor (no ephemeral waiver)
    assert status_of(conn, attached) == "pending"
    assert status_of(conn, kept) == "pending"


def test_llm_only_archive_auto_approves(conn, cfg):
    high = llm_only_row(conn, action="archive", confidence=0.97)
    low = llm_only_row(conn, action="archive", confidence=0.9, rfc_message_id="low@x")
    result = policy.apply_policy(conn, params(cfg))
    assert result["auto_archived"] == 1
    assert status_of(conn, high) == "auto_approved"
    assert status_of(conn, low) == "pending"


def test_gate_rejects_protect_won_rows(conn, cfg):
    msg_id = eligible_row(conn, rule_protected=1)
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_any_keep_voting_hit_blocks_auto_approval(conn, cfg):
    # The spam label outranked a keyword keep: the keep hit stays recorded and
    # must keep the message out of auto-approval no matter how confident the
    # LLM is — human review only.
    msg_id = eligible_row(conn, llm_confidence=0.99, rule_name="spam_label")
    add_rule_hit(conn, msg_id, "keep", "financial_legal_medical")
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_gate_requires_llm_confidence_by_default(conn, cfg):
    # A rule that decided alone (decision_source='rule', no LLM verdict) is
    # never auto-approved unless allow_rule_only is opted into.
    msg_id = eligible_row(
        conn, llm_action=None, llm_confidence=None, decision_source="rule"
    )
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_allow_rule_only_opts_rule_decided_trash_in(conn, cfg):
    msg_id = eligible_row(
        conn, llm_action=None, llm_confidence=None, decision_source="rule"
    )
    policy.apply_policy(conn, params(cfg, auto_trash_allow_rule_only=True))
    assert status_of(conn, msg_id) == "auto_approved"


def test_allow_rule_only_still_enforces_other_guards(conn, cfg):
    # The opt-in waives only the confidence requirement — age still applies.
    msg_id = eligible_row(
        conn,
        llm_action=None,
        llm_confidence=None,
        decision_source="rule",
        date_utc=RECENT_DATE.isoformat(),
        date_epoch=int(RECENT_DATE.timestamp()),
    )
    policy.apply_policy(conn, params(cfg, auto_trash_allow_rule_only=True))
    assert status_of(conn, msg_id) == "pending"


# --- ephemeral age-floor waiver ----------------------------------------------------


def test_ephemeral_recent_auto_trashes_when_both_agree(conn, cfg):
    # Recent (past the 7-day grace, far short of the 12-month floor), and BOTH
    # the rule and the LLM flagged it ephemeral: the age floor is waived.
    msg_id = eligible_row(
        conn,
        rule_ephemeral=1,
        llm_ephemeral=1,
        date_utc=RECENT_DATE.isoformat(),
        date_epoch=int(RECENT_DATE.timestamp()),
    )
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "auto_approved"


def test_ephemeral_needs_both_flags(conn, cfg):
    rule_only = eligible_row(
        conn,
        rule_ephemeral=1,
        llm_ephemeral=0,
        date_utc=RECENT_DATE.isoformat(),
        date_epoch=int(RECENT_DATE.timestamp()),
    )
    llm_only = eligible_row(
        conn,
        rule_ephemeral=0,
        llm_ephemeral=1,
        date_utc=RECENT_DATE.isoformat(),
        date_epoch=int(RECENT_DATE.timestamp()),
        rfc_message_id="llmonly@x",
    )
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, rule_only) == "pending"
    assert status_of(conn, llm_only) == "pending"


def test_ephemeral_within_grace_not_trashed(conn, cfg):
    # Inside the short grace window: even an ephemeral digest waits for review.
    just_now = datetime.now(UTC) - timedelta(days=2)
    msg_id = eligible_row(
        conn,
        rule_ephemeral=1,
        llm_ephemeral=1,
        date_utc=just_now.isoformat(),
        date_epoch=int(just_now.timestamp()),
    )
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_ephemeral_waives_only_age_not_other_conditions(conn, cfg):
    msg_id = eligible_row(
        conn,
        rule_ephemeral=1,
        llm_ephemeral=1,
        has_attachments=1,
        date_utc=RECENT_DATE.isoformat(),
        date_epoch=int(RECENT_DATE.timestamp()),
    )
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


# --- archive gate ------------------------------------------------------------------


def archive_row(conn, *, rule_hit: bool = True, **overrides) -> int:
    """A rule-staged archive candidate that passes the auto-archive gate."""
    fields = dict(
        ruled_at=RULED,
        rule_name="receipt",
        rule_action="archive",
        staged_action="archive",
        decision_source="rule",
        llm_confidence=None,
    )
    fields.update(overrides)
    msg_id = insert_message(conn, **fields)
    if rule_hit:
        add_rule_hit(conn, msg_id, "archive", "receipt", won=True)
    return msg_id


def test_archive_gate_approves_rule_decided_without_llm(conn, cfg):
    # NULL confidence (rule decided alone) counts as full confidence.
    msg_id = archive_row(conn)
    result = policy.apply_policy(conn, params(cfg))
    assert result["auto_archived"] == 1
    assert status_of(conn, msg_id) == "auto_approved"


def test_archive_gate_respects_llm_confidence_when_present(conn, cfg):
    low = archive_row(
        conn, llm_action="archive", llm_confidence=0.5, decision_source="rule+llm"
    )
    high = archive_row(
        conn,
        llm_action="archive",
        llm_confidence=0.85,
        decision_source="rule+llm",
        rfc_message_id="hi@x",
    )
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, low) == "pending"
    assert status_of(conn, high) == "auto_approved"


def test_archive_gate_requires_rule_or_the_llm_only_bar(conn, cfg):
    # Pure-LLM archive below the llm-only bar: no rule_action -> review only,
    # even though 0.9 clears the ordinary archive threshold (0.80).
    msg_id = insert_message(
        conn,
        ruled_at=RULED,
        llm_action="archive",
        llm_confidence=0.9,
        staged_action="archive",
        decision_source="llm",
    )
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_archive_gate_rejects_keep_hits(conn, cfg):
    msg_id = archive_row(conn)
    add_rule_hit(conn, msg_id, "keep", "financial_legal_medical")
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_archive_gate_rejects_known_contacts(conn, cfg):
    conn.execute(
        "INSERT INTO contacts (address, domain, sent_count) VALUES (?, 'example.com', 3)",
        (FRIEND_ADDR,),
    )
    msg_id = archive_row(conn, from_addr=FRIEND_ADDR)
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "pending"


def test_archive_gate_allows_attachments(conn, cfg):
    # Unlike trash: archiving keeps the message, so attachments don't block it.
    msg_id = archive_row(conn, has_attachments=1)
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "auto_approved"


def test_archive_gate_can_be_disabled(conn, cfg):
    # A threshold > 1 blocks LLM-scored AND rule-only (NULL confidence) rows.
    llm_row = archive_row(
        conn, llm_action="archive", llm_confidence=0.99, decision_source="rule+llm"
    )
    rules_row = archive_row(conn, rfc_message_id="r2@example.com")
    policy.apply_policy(conn, params(cfg, auto_archive_min_confidence=1.01))
    assert status_of(conn, llm_row) == "pending"
    assert status_of(conn, rules_row) == "pending"


# --- re-running & preview -----------------------------------------------------------


def test_rerun_demotes_on_stricter_threshold(conn, cfg):
    msg_id = eligible_row(conn, llm_confidence=0.92)
    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, msg_id) == "auto_approved"

    policy.apply_policy(conn, params(cfg, auto_trash_min_confidence=0.99))
    assert status_of(conn, msg_id) == "pending"


def test_rerun_never_touches_human_decisions(conn, cfg):
    approved = eligible_row(conn)
    rejected = eligible_row(conn, rfc_message_id="r2@example.com")
    conn.execute("UPDATE messages SET review_status='approved' WHERE id=?", (approved,))
    conn.execute("UPDATE messages SET review_status='rejected' WHERE id=?", (rejected,))
    conn.commit()

    policy.apply_policy(conn, params(cfg))
    assert status_of(conn, approved) == "approved"
    assert status_of(conn, rejected) == "rejected"


def test_preview_matches_apply(conn, cfg):
    eligible_row(conn)  # passes
    eligible_row(conn, llm_confidence=0.5, rfc_message_id="low@x")  # blocked
    archive_row(conn, rfc_message_id="arch@x")  # passes archive

    p = params(cfg)
    preview = policy.preview_policy(conn, p)
    result = policy.apply_policy(conn, p)
    assert preview.trash_count == result["auto_approved"] == 1
    assert preview.archive_count == result["auto_archived"] == 1
    assert len(preview.trash_sample) == 1
    assert preview.trash_sample[0]["rule_name"] == "promotional_label"


def test_preview_counts_already_auto_approved_rows(conn, cfg):
    # Preview after a gate run must show what a RE-run would approve (the gates
    # demote their own approvals first), not drop to zero.
    eligible_row(conn)
    p = params(cfg)
    policy.apply_policy(conn, p)
    assert policy.preview_policy(conn, p).trash_count == 1


def test_params_meta_roundtrip_and_precedence(conn, cfg):
    base = policy.PolicyParams.from_config(cfg)
    assert policy.PolicyParams.load(conn, cfg) == base  # nothing saved yet

    tuned = dataclasses.replace(
        base, auto_trash_min_confidence=0.97, auto_trash_allow_rule_only=True
    )
    tuned.save(conn)
    assert policy.PolicyParams.load(conn, cfg) == tuned  # meta wins over config
