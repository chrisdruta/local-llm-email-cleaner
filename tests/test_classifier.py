"""LLM classifier: routing, second-opinion semantics, failure handling,
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


def test_ambiguous_row_gets_llm_label(conn, cfg):
    msg_id = insert_message(conn, staged_label="NEEDS_REVIEW", proposed_action="review")
    stats = classifier.classify_messages(conn, cfg, fake_chain())
    assert stats.processed == 1

    row = row_of(conn, msg_id)
    assert row["staged_label"] == "DELETE_CANDIDATE"
    assert row["proposed_action"] == "trash"
    assert row["classified_by"] == "llm"
    assert row["ai_confidence"] == 0.97
    assert row["ai_reason"] == "old promo"


def test_delete_candidate_confirmed_by_llm(conn, cfg):
    msg_id = insert_message(
        conn,
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="rules",
    )
    classifier.classify_messages(conn, cfg, fake_chain(action="trash"))
    row = row_of(conn, msg_id)
    assert row["staged_label"] == "DELETE_CANDIDATE"
    assert row["classified_by"] == "rules+llm"


def test_delete_candidate_llm_disagreement_demotes_to_review(conn, cfg):
    msg_id = insert_message(
        conn,
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="rules",
    )
    classifier.classify_messages(conn, cfg, fake_chain(action="keep", confidence=0.8))
    row = row_of(conn, msg_id)
    assert row["staged_label"] == "NEEDS_REVIEW"
    assert row["proposed_action"] == "review"
    assert row["classified_by"] == "rules+llm"


def test_keep_rows_not_selected(conn, cfg):
    insert_message(
        conn, staged_label="KEEP", proposed_action="keep", classified_by="rules"
    )
    stats = classifier.classify_messages(conn, cfg, fake_chain())
    assert stats.processed == 0


def test_voice_delete_candidates_not_selected(conn, cfg):
    # The `voice` rule stages these DELETE_CANDIDATE but tags them 'voice'
    # (backed up to disk); the LLM can't meaningfully judge a text message, so
    # the classifier must skip them. A rules-staged delete candidate still runs.
    insert_message(
        conn,
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="voice",
    )
    rule_id = insert_message(
        conn,
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="rules",
    )
    stats = classifier.classify_messages(conn, cfg, fake_chain())
    assert stats.processed == 1
    assert row_of(conn, rule_id)["classified_by"] == "rules+llm"


def archive_candidate(conn, **overrides) -> int:
    fields = dict(
        staged_label="ARCHIVE_CANDIDATE",
        proposed_action="archive",
        classified_by="rules",
    )
    fields.update(overrides)
    return insert_message(conn, **fields)


def test_archive_candidate_confirmed_by_llm(conn, cfg):
    # LLM agrees it's archive-worthy: stays archive, now with a confidence.
    msg_id = archive_candidate(conn)
    classifier.classify_messages(
        conn, cfg, fake_chain(action="archive", confidence=0.85)
    )
    row = row_of(conn, msg_id)
    assert row["staged_label"] == "ARCHIVE_CANDIDATE"
    assert row["proposed_action"] == "archive"
    assert row["classified_by"] == "rules+llm"
    assert row["ai_confidence"] == 0.85


def test_archive_candidate_escalated_to_trash_by_llm(conn, cfg):
    # LLM thinks it's outright junk: hand it to the auto-trash gate.
    msg_id = archive_candidate(conn)
    classifier.classify_messages(conn, cfg, fake_chain(action="trash", confidence=0.97))
    row = row_of(conn, msg_id)
    assert row["staged_label"] == "DELETE_CANDIDATE"
    assert row["proposed_action"] == "trash"
    assert row["classified_by"] == "rules+llm"


def test_archive_candidate_llm_disagreement_demotes_to_review(conn, cfg):
    # LLM says keep: conservative -> human review.
    msg_id = archive_candidate(conn)
    classifier.classify_messages(conn, cfg, fake_chain(action="keep", confidence=0.7))
    row = row_of(conn, msg_id)
    assert row["staged_label"] == "NEEDS_REVIEW"
    assert row["proposed_action"] == "review"
    assert row["classified_by"] == "rules+llm"


def test_llm_alone_cannot_set_ephemeral(conn, cfg):
    # The age-floor waiver requires BOTH signals. This row never hit the
    # deterministic `digest` rule (ephemeral starts 0), so even when the LLM
    # escalates to trash and claims ephemeral, the flag must stay 0 — the LLM
    # alone may not waive the 12-month floor.
    msg_id = archive_candidate(conn)
    classifier.classify_messages(
        conn, cfg, fake_chain(action="trash", category="digest", ephemeral=True)
    )
    row = row_of(conn, msg_id)
    assert row["staged_label"] == "DELETE_CANDIDATE"
    assert row["proposed_action"] == "trash"
    assert row["ephemeral"] == 0


def test_ephemeral_requires_llm_confirmation(conn, cfg):
    # The digest rule flagged this ephemeral, but the LLM second opinion does
    # NOT consider it ephemeral: require-both -> the flag is cleared, so it
    # falls back to the normal age floor.
    msg_id = insert_message(
        conn,
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="rules",
        ephemeral=1,
    )
    classifier.classify_messages(conn, cfg, fake_chain(action="trash", ephemeral=False))
    assert row_of(conn, msg_id)["ephemeral"] == 0


def test_ephemeral_set_only_when_both_agree(conn, cfg):
    # Digest rule set ephemeral AND the LLM confirms it -> the waiver applies.
    msg_id = insert_message(
        conn,
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="rules",
        ephemeral=1,
    )
    classifier.classify_messages(conn, cfg, fake_chain(action="trash", ephemeral=True))
    assert row_of(conn, msg_id)["ephemeral"] == 1


def test_non_ephemeral_classification_leaves_flag_zero(conn, cfg):
    msg_id = insert_message(conn, staged_label="NEEDS_REVIEW", proposed_action="review")
    classifier.classify_messages(conn, cfg, fake_chain(ephemeral=False))
    assert row_of(conn, msg_id)["ephemeral"] == 0


def test_pending_predicate_matches_what_classify_processes(conn, cfg):
    """status counts the SAME population classify selects: voice excluded,
    archive candidates included, and it reads 0 after a full run."""
    from local_llm_email_cleaner import models

    insert_message(conn, staged_label="NEEDS_REVIEW", proposed_action="review")
    insert_message(
        conn,
        staged_label="ARCHIVE_CANDIDATE",
        proposed_action="archive",
        classified_by="rules",
        rfc_message_id="arch@x",
    )
    insert_message(
        conn,
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by="voice",  # decided by the export -> never sent to the LLM
        rfc_message_id="voice@x",
    )

    def pending_count() -> int:
        return conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {models.PENDING_CLASSIFICATION_WHERE}",
            models.pending_classification_params(),
        ).fetchone()[0]

    assert pending_count() == 2  # the review + archive rows, NOT the voice row
    stats = classifier.classify_messages(conn, cfg, fake_chain(action="archive"))
    assert stats.processed == 2
    assert pending_count() == 0  # nothing lingers (the voice row never counted)


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

    msg_id = insert_message(conn, staged_label="NEEDS_REVIEW", proposed_action="review")
    stats = classifier.classify_messages(conn, cfg, RunnableLambda(boom))
    assert stats.failed == 1

    row = row_of(conn, msg_id)
    assert row["staged_label"] == "NEEDS_REVIEW"
    assert row["ai_confidence"] == 0.0
    assert "classification failed" in row["ai_reason"]


def test_resume_skips_already_classified(conn, cfg):
    insert_message(conn, staged_label="NEEDS_REVIEW", proposed_action="review")
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

    msg_id = insert_message(conn, staged_label="NEEDS_REVIEW", proposed_action="review")
    stats = classifier.classify_messages(conn, cfg, RunnableLambda(flaky))
    assert stats.processed == 1
    assert stats.failed == 0
    assert row_of(conn, msg_id)["ai_confidence"] == 0.95


def test_mixed_batch_isolates_failures(conn, cfg, monkeypatch):
    monkeypatch.setattr(classifier.time, "sleep", lambda _s: None)

    def per_subject(inputs):
        if inputs["subject"] == "bad":
            raise RuntimeError("ollama exploded")
        return EmailClassification(
            action="trash", category="promotion", confidence=0.95, reason="junk"
        )

    good_id = insert_message(
        conn, staged_label="NEEDS_REVIEW", proposed_action="review", subject="good"
    )
    bad_id = insert_message(
        conn,
        staged_label="NEEDS_REVIEW",
        proposed_action="review",
        subject="bad",
        rfc_message_id="bad@x",
    )
    stats = classifier.classify_messages(conn, cfg, RunnableLambda(per_subject))
    assert stats.processed == 1
    assert stats.failed == 1
    assert row_of(conn, good_id)["staged_label"] == "DELETE_CANDIDATE"
    bad_row = row_of(conn, bad_id)
    assert bad_row["staged_label"] == "NEEDS_REVIEW"
    assert bad_row["ai_confidence"] == 0.0


def test_requests_run_concurrently(conn, cfg):
    # Both classifications must be in flight at once for the barrier to
    # release; a sequential implementation times out and fails both rows.
    barrier = threading.Barrier(2, timeout=10)

    def rendezvous(_inputs):
        barrier.wait()
        return EmailClassification(
            action="trash", category="promotion", confidence=0.95, reason="junk"
        )

    insert_message(conn, staged_label="NEEDS_REVIEW", proposed_action="review")
    insert_message(
        conn,
        staged_label="NEEDS_REVIEW",
        proposed_action="review",
        rfc_message_id="b@x",
    )
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
        insert_message(
            conn,
            staged_label="NEEDS_REVIEW",
            proposed_action="review",
            rfc_message_id=f"m{i}@x",
        )
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

    done_id = insert_message(
        conn, staged_label="NEEDS_REVIEW", proposed_action="review", subject="ok"
    )
    hit_id = insert_message(
        conn,
        staged_label="NEEDS_REVIEW",
        proposed_action="review",
        subject="interrupt",
        rfc_message_id="int@x",
    )
    cfg = dataclasses.replace(cfg, llm_concurrency=1)  # deterministic order
    with pytest.raises(KeyboardInterrupt):
        classifier.classify_messages(conn, cfg, RunnableLambda(per_subject))

    # The finished row is durable (visible from a fresh connection) ...
    other = sqlite3.connect(cfg.db_path)
    other.row_factory = sqlite3.Row
    done_row = other.execute("SELECT * FROM messages WHERE id=?", (done_id,)).fetchone()
    hit_row = other.execute("SELECT * FROM messages WHERE id=?", (hit_id,)).fetchone()
    other.close()
    assert done_row["ai_confidence"] == 0.95
    assert done_row["staged_label"] == "DELETE_CANDIDATE"
    # ... and the interrupted row is untouched, so the next run picks it up.
    assert hit_row["ai_confidence"] is None
    assert hit_row["staged_label"] == "NEEDS_REVIEW"


def test_progress_ticks_per_message(conn, cfg):
    """progress fires per finalized message, not per batch chunk."""
    for i in range(3):
        insert_message(
            conn,
            staged_label="NEEDS_REVIEW",
            proposed_action="review",
            rfc_message_id=f"p{i}@x",
        )
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

    insert_message(conn, staged_label="NEEDS_REVIEW", proposed_action="review")
    seen = []
    stats = classifier.classify_messages(
        conn,
        cfg,
        RunnableLambda(boom),
        progress=lambda s, total: seen.append((s.processed, s.failed)),
    )
    assert stats.failed == 1
    assert seen == [(0, 1), (0, 1)]
