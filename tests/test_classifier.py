"""LLM classifier: selection, agree/disagree finalization, failure handling,
resume, and concurrent fan-out."""

from __future__ import annotations

import dataclasses
import sqlite3
import threading

import pytest
from langchain_core.runnables import RunnableLambda

from conftest import insert_message

from local_llm_email_cleaner.config import DEFAULTS
from local_llm_email_cleaner.llm import classifier
from local_llm_email_cleaner.llm.schema import EmailClassification

RULED = "2026-01-01T00:00:00"


def fake_chain(
    action="trash",
    category="promotion",
    confidence=0.97,
    reason="old promo",
    ephemeral=False,
):
    return RunnableLambda(
        lambda _inputs: EmailClassification(
            action=action,
            category=category,
            confidence=confidence,
            reason=reason,
            ephemeral=ephemeral,
        )
    )


def row_of(conn, msg_id):
    return conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()


def no_rule_row(conn, **overrides) -> int:
    """Ruled, nothing matched: the LLM is the primary classifier."""
    return insert_message(conn, ruled_at=RULED, **overrides)


def rule_staged_row(conn, rule_action="trash", **overrides) -> int:
    """Ruled by a confirm_with_llm rule: awaiting the LLM's second opinion."""
    return insert_message(
        conn,
        ruled_at=RULED,
        rule_name="promotional_label",
        rule_action=rule_action,
        rule_category="promotion",
        **overrides,
    )


# --- selection -------------------------------------------------------------------


def test_unruled_rows_not_selected(conn, cfg):
    insert_message(conn)  # ruled_at NULL: rules haven't run yet
    assert classifier.classify_messages(conn, cfg, fake_chain()).processed == 0


def test_finalized_rows_not_selected(conn, cfg):
    # A rule that decided alone (e.g. voice, or any confirm_with_llm=false).
    insert_message(
        conn,
        ruled_at=RULED,
        rule_name="voice",
        rule_action="trash",
        action="trash",
        decision_source="rule",
    )
    assert classifier.classify_messages(conn, cfg, fake_chain()).processed == 0


def test_selection_is_action_null_and_ruled(conn, cfg):
    no_rule_row(conn)
    rule_staged_row(conn, rfc_message_id="staged@x")

    def awaiting() -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM messages "
            "WHERE action IS NULL AND ruled_at IS NOT NULL"
        ).fetchone()[0]

    assert awaiting() == 2
    assert classifier.classify_messages(conn, cfg, fake_chain()).processed == 2
    assert awaiting() == 0  # nothing lingers


# --- finalization ----------------------------------------------------------------


def test_no_rule_match_takes_llm_action(conn, cfg):
    msg_id = no_rule_row(conn)
    stats = classifier.classify_messages(conn, cfg, fake_chain())
    assert stats.processed == 1

    row = row_of(conn, msg_id)
    assert row["llm_action"] == "trash"
    assert row["llm_category"] == "promotion"
    assert row["llm_confidence"] == 0.97
    assert row["llm_reason"] == "old promo"
    assert row["action"] == "trash"
    assert row["decision_source"] == "llm"


def test_rule_confirmed_by_llm(conn, cfg):
    msg_id = rule_staged_row(conn, rule_action="trash")
    classifier.classify_messages(conn, cfg, fake_chain(action="trash"))
    row = row_of(conn, msg_id)
    assert row["action"] == "trash"
    assert row["decision_source"] == "rule+llm"


def test_rule_disagreement_routes_to_review(conn, cfg):
    msg_id = rule_staged_row(conn, rule_action="trash")
    classifier.classify_messages(conn, cfg, fake_chain(action="keep", confidence=0.8))
    row = row_of(conn, msg_id)
    assert row["llm_action"] == "keep"  # the verdict is recorded verbatim
    assert row["action"] == "review"  # but a human decides
    assert row["decision_source"] == "rule+llm"


def test_archive_rule_llm_trash_is_disagreement(conn, cfg):
    # v3 is strict: no archive->trash escalation; any mismatch goes to review.
    msg_id = rule_staged_row(conn, rule_action="archive")
    classifier.classify_messages(conn, cfg, fake_chain(action="trash"))
    row = row_of(conn, msg_id)
    assert row["action"] == "review"
    assert row["decision_source"] == "rule+llm"


def test_keep_rule_confirmed_stays_kept(conn, cfg):
    # A keyword keep (confirm_with_llm) the LLM agrees with.
    msg_id = rule_staged_row(conn, rule_action="keep")
    classifier.classify_messages(
        conn, cfg, fake_chain(action="keep", category="financial_legal_medical")
    )
    row = row_of(conn, msg_id)
    assert row["action"] == "keep"
    assert row["decision_source"] == "rule+llm"


def test_keep_rule_llm_trash_routes_to_review(conn, cfg):
    # The keyword rule over-matched a promo footer; the LLM calls it junk.
    # v3 routes the dispute to a human instead of directly downgrading.
    msg_id = rule_staged_row(conn, rule_action="keep")
    classifier.classify_messages(conn, cfg, fake_chain(action="trash"))
    row = row_of(conn, msg_id)
    assert row["llm_action"] == "trash"
    assert row["action"] == "review"


def test_llm_review_verdict_stands_for_no_rule_rows(conn, cfg):
    msg_id = no_rule_row(conn)
    classifier.classify_messages(conn, cfg, fake_chain(action="review", confidence=0.4))
    row = row_of(conn, msg_id)
    assert row["action"] == "review"
    assert row["decision_source"] == "llm"


def test_llm_ephemeral_recorded_verbatim(conn, cfg):
    # The AND-semantics live in the policy gate (rule_ephemeral AND
    # llm_ephemeral); the classifier just records the LLM's own judgment.
    msg_id = rule_staged_row(conn, rule_action="trash")
    classifier.classify_messages(
        conn, cfg, fake_chain(action="trash", category="digest", ephemeral=True)
    )
    row = row_of(conn, msg_id)
    assert row["llm_ephemeral"] == 1
    assert row["rule_ephemeral"] == 0  # untouched — owned by the rules stage


# --- plumbing ---------------------------------------------------------------------


def test_chain_passes_request_timeout(monkeypatch):
    """request_timeout_s must reach the ollama client, not be silently dropped."""
    from local_llm_email_cleaner.llm import chain as chain_mod

    captured = {}

    class RecordingOllama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def with_structured_output(self, *a, **k):
            return RunnableLambda(lambda x: x)  # coercible for the `prompt | llm` pipe

    monkeypatch.setattr(chain_mod, "ChatOllama", RecordingOllama)
    cfg = dataclasses.replace(DEFAULTS, request_timeout_s=42.0)
    chain_mod.build_classifier_chain(cfg)
    assert captured["client_kwargs"] == {"timeout": 42.0}


def test_classification_rejects_unknown_category():
    # The category enum is constrained; an off-vocabulary slug is invalid.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EmailClassification(
            action="trash", category="totally_made_up", confidence=0.9, reason="x"
        )


def test_failure_marks_row_for_human_review(conn, cfg, monkeypatch):
    monkeypatch.setattr(classifier.time, "sleep", lambda _s: None)  # skip retry backoff

    def boom(_inputs):
        raise RuntimeError("ollama exploded")

    msg_id = no_rule_row(conn)
    stats = classifier.classify_messages(conn, cfg, RunnableLambda(boom))
    assert stats.failed == 1

    row = row_of(conn, msg_id)
    assert row["action"] == "review"
    assert row["llm_confidence"] == 0.0
    assert "classification failed" in row["llm_reason"]


def test_resume_skips_already_classified(conn, cfg):
    no_rule_row(conn)
    assert classifier.classify_messages(conn, cfg, fake_chain()).processed == 1
    # Second run finds nothing left to do.
    assert classifier.classify_messages(conn, cfg, fake_chain()).processed == 0


def test_transient_failure_recovers_on_retry(conn, cfg, monkeypatch):
    monkeypatch.setattr(classifier.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky(_inputs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return EmailClassification(
            action="trash", category="promotion", confidence=0.95, reason="junk"
        )

    msg_id = no_rule_row(conn)
    stats = classifier.classify_messages(conn, cfg, RunnableLambda(flaky))
    assert stats.processed == 1
    assert stats.failed == 0
    assert row_of(conn, msg_id)["llm_confidence"] == 0.95


def test_mixed_batch_isolates_failures(conn, cfg, monkeypatch):
    monkeypatch.setattr(classifier.time, "sleep", lambda _s: None)

    def per_subject(inputs):
        if inputs["subject"] == "bad":
            raise RuntimeError("ollama exploded")
        return EmailClassification(
            action="trash", category="promotion", confidence=0.95, reason="junk"
        )

    good_id = no_rule_row(conn, subject="good")
    bad_id = no_rule_row(conn, subject="bad", rfc_message_id="bad@x")
    stats = classifier.classify_messages(conn, cfg, RunnableLambda(per_subject))
    assert stats.processed == 1
    assert stats.failed == 1
    assert row_of(conn, good_id)["action"] == "trash"
    bad_row = row_of(conn, bad_id)
    assert bad_row["action"] == "review"
    assert bad_row["llm_confidence"] == 0.0


def test_requests_run_concurrently(conn, cfg):
    # Both classifications must be in flight at once for the barrier to
    # release; a sequential implementation times out and fails both rows.
    barrier = threading.Barrier(2, timeout=10)

    def rendezvous(_inputs):
        barrier.wait()
        return EmailClassification(
            action="trash", category="promotion", confidence=0.95, reason="junk"
        )

    no_rule_row(conn)
    no_rule_row(conn, rfc_message_id="b@x")
    cfg = dataclasses.replace(cfg, llm_concurrency=2)
    stats = classifier.classify_messages(conn, cfg, RunnableLambda(rendezvous))
    assert stats.processed == 2
    assert stats.failed == 0


def test_commits_per_chunk(conn, cfg, monkeypatch):
    """llm_batch_size bounds the chunk handed to _batch_with_retry."""
    sizes = []
    original = classifier._batch_with_retry

    def spy(chain, inputs, concurrency, on_done=None):
        sizes.append(len(inputs))
        return original(chain, inputs, concurrency, on_done)

    monkeypatch.setattr(classifier, "_batch_with_retry", spy)
    for i in range(3):
        no_rule_row(conn, rfc_message_id=f"m{i}@x")
    cfg = dataclasses.replace(cfg, llm_batch_size=2)
    stats = classifier.classify_messages(
        conn,
        cfg,
        fake_chain(action="keep", category="personal", confidence=0.6, reason="ok"),
    )
    assert stats.processed == 3
    assert sizes == [2, 1]


def test_interrupt_commits_finished_work_and_drops_the_rest(conn, cfg):
    """Ctrl-C mid-chunk: completed rows are committed, unfinished stay NULL."""

    def per_subject(inputs):
        if inputs["subject"] == "interrupt":
            raise KeyboardInterrupt
        return EmailClassification(
            action="trash", category="promotion", confidence=0.95, reason="junk"
        )

    done_id = no_rule_row(conn, subject="ok")
    hit_id = no_rule_row(conn, subject="interrupt", rfc_message_id="int@x")
    cfg = dataclasses.replace(cfg, llm_concurrency=1)  # deterministic order
    with pytest.raises(KeyboardInterrupt):
        classifier.classify_messages(conn, cfg, RunnableLambda(per_subject))

    # The finished row is durable (visible from a fresh connection) ...
    other = sqlite3.connect(cfg.db_path)
    other.row_factory = sqlite3.Row
    done_row = other.execute("SELECT * FROM messages WHERE id=?", (done_id,)).fetchone()
    hit_row = other.execute("SELECT * FROM messages WHERE id=?", (hit_id,)).fetchone()
    other.close()
    assert done_row["llm_confidence"] == 0.95
    assert done_row["action"] == "trash"
    # ... and the interrupted row is untouched, so the next run picks it up.
    assert hit_row["llm_confidence"] is None
    assert hit_row["action"] is None


def test_progress_ticks_per_message(conn, cfg):
    """progress fires per finalized message, not per batch chunk."""
    for i in range(3):
        no_rule_row(conn, rfc_message_id=f"p{i}@x")
    seen = []
    classifier.classify_messages(
        conn,
        cfg,
        fake_chain(),
        progress=lambda s, total: seen.append((s.processed + s.failed, total)),
    )
    # One tick per message plus the final flush, all against the same total.
    assert seen == [(1, 3), (2, 3), (3, 3), (3, 3)]


def test_progress_counts_failures_once(conn, cfg, monkeypatch):
    """A row that fails all retries ticks progress exactly once, at the end."""
    monkeypatch.setattr(classifier.time, "sleep", lambda _s: None)

    def boom(_inputs):
        raise RuntimeError("ollama exploded")

    no_rule_row(conn)
    seen = []
    stats = classifier.classify_messages(
        conn,
        cfg,
        RunnableLambda(boom),
        progress=lambda s, total: seen.append((s.processed, s.failed)),
    )
    assert stats.failed == 1
    assert seen == [(0, 1), (0, 1)]
