"""Rules engine: protection wins, candidates staged, hits recorded."""

from __future__ import annotations

from conftest import FRIEND_ADDR

from local_llm_email_cleaner.ingest import contacts, store
from local_llm_email_cleaner.models import StagedLabel
from local_llm_email_cleaner.rules import engine
from local_llm_email_cleaner.rules.views import MessageView, RuleContext


def make_view(**overrides) -> MessageView:
    defaults = dict(
        id=1,
        from_addr="x@example.com",
        from_name=None,
        subject="hello",
        labels=frozenset(),
        has_attachments=False,
        list_unsubscribe=False,
    )
    defaults.update(overrides)
    return MessageView(**defaults)


class TestEvaluateMessage:
    ctx = RuleContext(known_contacts=frozenset({FRIEND_ADDR}))

    def test_known_contact_protected(self):
        result = engine.evaluate_message(make_view(from_addr=FRIEND_ADDR), self.ctx)
        assert result.staged_label == StagedLabel.KEEP
        assert any(h.rule_name == "known_contact" for h in result.hits)

    def test_financial_subject_protected(self):
        result = engine.evaluate_message(
            make_view(subject="Your 2023 tax documents are ready"), self.ctx
        )
        assert result.staged_label == StagedLabel.KEEP
        assert result.category == "financial_legal_medical"

    def test_security_alert_protected(self):
        result = engine.evaluate_message(
            make_view(subject="Security alert: new sign-in from Windows"), self.ctx
        )
        assert result.staged_label == StagedLabel.KEEP

    def test_protection_beats_candidates(self):
        # A promo from a known contact stays KEEP.
        result = engine.evaluate_message(
            make_view(from_addr=FRIEND_ADDR, labels=frozenset({"category promotions"})),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.KEEP

    def test_spam_overrides_keyword_protection_but_records_hit(self):
        # Legal-bait subject on Gmail-flagged spam: staged for deletion, but
        # the protection hit is kept so the policy gates can never auto-approve.
        result = engine.evaluate_message(
            make_view(subject="Final legal notice", labels=frozenset({"spam"})),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.DELETE_CANDIDATE
        names = {h.rule_name for h in result.hits}
        assert {"financial_legal_medical", "spam_label"} <= names

    def test_spam_never_overrides_known_contact(self):
        result = engine.evaluate_message(
            make_view(from_addr=FRIEND_ADDR, labels=frozenset({"spam"})), self.ctx
        )
        assert result.staged_label == StagedLabel.KEEP

    def test_spam_label_is_delete_candidate(self):
        result = engine.evaluate_message(
            make_view(labels=frozenset({"spam"})), self.ctx
        )
        assert result.staged_label == StagedLabel.DELETE_CANDIDATE
        assert result.category == "spam"
        assert any(h.rule_name == "spam_label" for h in result.hits)

    def test_promo_label_is_delete_candidate(self):
        result = engine.evaluate_message(
            make_view(labels=frozenset({"category promotions"}), list_unsubscribe=True),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.DELETE_CANDIDATE
        names = {h.rule_name for h in result.hits}
        assert {"promotional_label", "newsletter_unsubscribe"} <= names

    def test_receipt_archives_over_delete(self):
        # Receipt + promo label: most conservative candidate (archive) wins.
        result = engine.evaluate_message(
            make_view(
                subject="Receipt for your purchase",
                labels=frozenset({"category promotions"}),
            ),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.ARCHIVE_CANDIDATE

    def test_unsubscribe_only_when_nothing_else(self):
        result = engine.evaluate_message(make_view(list_unsubscribe=True), self.ctx)
        assert result.staged_label == StagedLabel.UNSUBSCRIBE_CANDIDATE

    def test_no_hits_needs_review(self):
        result = engine.evaluate_message(make_view(), self.ctx)
        assert result.staged_label == StagedLabel.NEEDS_REVIEW
        assert result.hits == ()


def test_run_rules_end_to_end(conn, mbox_path, cfg):
    store.ingest_mbox(conn, mbox_path)
    contacts.derive_contacts(conn, cfg.user_addresses)
    ctx = engine.load_context(conn)

    counts = engine.run_rules(conn, ctx)
    assert sum(counts.values()) == 7

    def staged(message_id_suffix: str) -> str:
        return conn.execute(
            "SELECT staged_label FROM messages WHERE rfc_message_id=?",
            (message_id_suffix,),
        ).fetchone()[0]

    assert staged("friend-1@example.com") == "KEEP"  # known contact
    assert staged("bank-1@example.com") == "KEEP"  # financial keywords
    # Promos trash regardless of age (promotional_label -> DELETE_CANDIDATE).
    assert staged("promo-1@example.com") == "DELETE_CANDIDATE"
    assert staged("promo-2@example.com") == "DELETE_CANDIDATE"
    assert staged("ship-1@example.com") == "DELETE_CANDIDATE"
    assert staged("photos-1@example.com") == "NEEDS_REVIEW"

    # rule_hits recorded with kinds
    hits = conn.execute(
        """
        SELECT rule_name, rule_kind FROM rule_hits
        JOIN messages ON messages.id = rule_hits.message_id
        WHERE messages.rfc_message_id = 'ship-1@example.com'
        """
    ).fetchall()
    names = {(h["rule_name"], h["rule_kind"]) for h in hits}
    assert ("shipping", "candidate") in names
    assert ("noreply_sender", "candidate") in names

    # Second run is a no-op (only un-ruled rows are evaluated).
    assert sum(engine.run_rules(conn, ctx).values()) == 0


def test_reset_preserves_non_pending_rule_hits(conn, mbox_path):
    """rules --reset must not orphan approved/applied rows from their hits."""
    store.ingest_mbox(conn, mbox_path)
    ctx = engine.load_context(conn)
    engine.run_rules(conn, ctx)

    approved_id = conn.execute(
        "SELECT id FROM messages WHERE rfc_message_id='ship-1@example.com'"
    ).fetchone()[0]
    conn.execute(
        "UPDATE messages SET review_status='approved' WHERE id=?", (approved_id,)
    )
    conn.commit()

    def hits(message_id: int) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM rule_hits WHERE message_id=?", (message_id,)
        ).fetchone()[0]

    assert hits(approved_id) > 0
    pending_id = conn.execute(
        "SELECT id FROM messages WHERE rfc_message_id='promo-1@example.com'"
    ).fetchone()[0]
    assert hits(pending_id) > 0

    engine.run_rules(conn, ctx, reset=True)

    # Approved row keeps its hits and its staging; pending row was recomputed.
    assert hits(approved_id) > 0
    assert hits(pending_id) > 0
    assert (
        conn.execute(
            "SELECT staged_label FROM messages WHERE id=?", (approved_id,)
        ).fetchone()[0]
        is not None
    )


def test_run_rules_commits_per_chunk(conn, mbox_path, monkeypatch):
    store.ingest_mbox(conn, mbox_path)
    ctx = engine.load_context(conn)
    monkeypatch.setattr(engine, "BATCH_SIZE", 2)
    counts = engine.run_rules(conn, ctx)
    assert sum(counts.values()) == 7
    staged_rows = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE staged_label IS NOT NULL"
    ).fetchone()[0]
    assert staged_rows == 7
