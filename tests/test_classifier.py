"""LLM classifier: routing, second-opinion semantics, failure handling,
resume, and concurrent fan-out."""

from __future__ import annotations

import dataclasses
import sqlite3
import threading

import pytest
from langchain_core.runnables import RunnableLambda

from conftest import insert_message

from local_llm_email_cleaner.llm import classifier
from local_llm_email_cleaner.llm.schema import EmailClassification


def fake_chain(
    action="trash", category="promotion", confidence=0.97, reason="old promo"
):
    return RunnableLambda(
        lambda _inputs: EmailClassification(
            action=action, category=category, confidence=confidence, reason=reason
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


def test_keep_and_archive_rows_not_selected(conn, cfg):
    insert_message(
        conn, staged_label="KEEP", proposed_action="keep", classified_by="rules"
    )
    insert_message(
        conn,
        staged_label="ARCHIVE_CANDIDATE",
        proposed_action="archive",
        classified_by="rules",
        rfc_message_id="a@x",
    )
    stats = classifier.classify_messages(conn, cfg, fake_chain())
    assert stats.processed == 0


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
