"""Rules engine: protection wins, candidates staged, hits recorded."""

from __future__ import annotations

from conftest import FRIEND_ADDR

from local_llm_email_cleaner.ingest import contacts, store
from local_llm_email_cleaner.models import CLASSIFIED_BY_VOICE, StagedLabel
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

    def test_financial_body_protected_despite_innocuous_subject(self):
        # The sensitive substance is only in the body; a noreply sender would
        # otherwise stage this DELETE_CANDIDATE. Body scan -> KEEP.
        result = engine.evaluate_message(
            make_view(
                from_addr="noreply@bank.example",
                subject="Your monthly update is ready",
                body_text="Your bank statement for account ending 1234 is now available.",
            ),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.KEEP
        assert result.category == "financial_legal_medical"

    def test_security_body_protected_despite_innocuous_subject(self):
        result = engine.evaluate_message(
            make_view(
                from_addr="noreply@service.example",
                subject="A note for you",
                body_text="Your verification code is 558213. Do not share it.",
            ),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.KEEP

    def test_financial_body_ignores_promo_footer_keywords(self):
        # A clearance promo whose body only carries bare footer words ("legal",
        # "insurance", a lone "tax", "benefits") must NOT be protected — the
        # body match needs an unambiguous multi-word phrase. With no candidate
        # rule firing either, it falls through to NEEDS_REVIEW (the LLM decides).
        result = engine.evaluate_message(
            make_view(
                from_addr="deals@wayfair.example",
                subject="🔴 MEMORIAL DAY CLEARANCE 🔴",
                body_text=(
                    "Shop our best deals! 0% financing available. See our legal "
                    "terms. Tax-free weekend. Member benefits apply. Insurance "
                    "not included."
                ),
            ),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.NEEDS_REVIEW

    def test_security_body_ignores_reset_password_cta(self):
        # The account-management CTA "reset your password" pervades promo
        # footers; on its own in the body it must not protect a marketing blast.
        result = engine.evaluate_message(
            make_view(
                from_addr="hello@alphalete.example",
                subject="A tee you buy twice 🔥",
                body_text=(
                    "New drop is live! Manage your account or reset your password "
                    "any time from your profile."
                ),
            ),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.NEEDS_REVIEW

    def test_security_body_still_protects_password_change_notice(self):
        # The narrowed body pattern keeps the genuine past-tense notification.
        result = engine.evaluate_message(
            make_view(
                from_addr="noreply@service.example",
                subject="A note for you",
                body_text="Your password was changed on a new device.",
            ),
            self.ctx,
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

    def test_digest_sender_is_ephemeral_delete(self):
        # A Reddit-style digest also hits updates_label (Forums -> ARCHIVE), but
        # the digest override wins: DELETE_CANDIDATE, category 'digest', ephemeral.
        result = engine.evaluate_message(
            make_view(
                from_addr="noreply@redditmail.com",
                subject="Top posts from your communities",
                labels=frozenset({"category forums"}),
                list_unsubscribe=True,
            ),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.DELETE_CANDIDATE
        assert result.category == "digest"
        assert result.ephemeral is True
        assert any(h.rule_name == "digest" for h in result.hits)

    def test_digest_subject_backstop(self):
        # Sender-agnostic: a digest-shaped subject alone triggers it.
        result = engine.evaluate_message(
            make_view(from_addr="news@somesite.example", subject="Your daily digest"),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.DELETE_CANDIDATE
        assert result.ephemeral is True

    def test_digest_never_overrides_protection(self):
        # A digest from a known contact stays KEEP (protection runs first).
        result = engine.evaluate_message(
            make_view(from_addr=FRIEND_ADDR, subject="Your weekly digest"),
            self.ctx,
        )
        assert result.staged_label == StagedLabel.KEEP
        assert result.ephemeral is False

    def test_non_digest_is_not_ephemeral(self):
        result = engine.evaluate_message(
            make_view(labels=frozenset({"category promotions"})), self.ctx
        )
        assert result.ephemeral is False

    def test_voice_sms_is_delete_candidate_tagged_voice(self):
        # Google Voice records are staged for trash and tagged 'voice' so the
        # LLM classifier skips them (CLASSIFIED_BY_VOICE).
        result = engine.evaluate_message(make_view(labels=frozenset({"sms"})), self.ctx)
        assert result.staged_label == StagedLabel.DELETE_CANDIDATE
        assert result.category == "voice_sms"
        assert result.classified_by == CLASSIFIED_BY_VOICE
        assert any(h.rule_name == "voice" for h in result.hits)

    def test_voice_call_log_and_voicemail_categories(self):
        call = engine.evaluate_message(
            make_view(labels=frozenset({"call log"})), self.ctx
        )
        assert call.staged_label == StagedLabel.DELETE_CANDIDATE
        assert call.category == "voice_call"

        vm = engine.evaluate_message(
            make_view(labels=frozenset({"voicemail"})), self.ctx
        )
        assert vm.category == "voice_voicemail"

    def test_voice_beats_other_candidates(self):
        # An SMS that also carries an archive-class label still trashes as voice
        # (the override wins over the most-conservative precedence).
        result = engine.evaluate_message(
            make_view(labels=frozenset({"sms", "category updates"})), self.ctx
        )
        assert result.staged_label == StagedLabel.DELETE_CANDIDATE
        assert result.category == "voice_sms"
        assert result.classified_by == CLASSIFIED_BY_VOICE

    def test_voice_overrides_protection(self):
        # A Voice record is decided before protection: even when the (synthetic
        # `unknown.email`) number leaked into the contacts, it still trashes as
        # voice rather than being shielded as a known contact.
        result = engine.evaluate_message(
            make_view(from_addr=FRIEND_ADDR, labels=frozenset({"sms"})), self.ctx
        )
        assert result.staged_label == StagedLabel.DELETE_CANDIDATE
        assert result.category == "voice_sms"
        assert result.classified_by == CLASSIFIED_BY_VOICE
        # The shielding known_contact protection is not even recorded.
        assert not any(h.rule_name == "known_contact" for h in result.hits)


def test_select_candidate_is_priority_then_precedence():
    """Candidate selection is data-driven: highest priority wins, ties broken by
    the conservative precedence — no rule is matched by name."""
    from local_llm_email_cleaner.models import RuleKind, RuleVote

    def vote(name, label, priority=0):
        return RuleVote(name, RuleKind.CANDIDATE, label, name, priority=priority)

    # Higher priority wins outright, even over a more-conservative label.
    winner = engine._select_candidate(
        (
            vote("arch", StagedLabel.ARCHIVE_CANDIDATE),
            vote("hi", StagedLabel.DELETE_CANDIDATE, priority=5),
        )
    )
    assert winner.rule_name == "hi"

    # Equal priority -> most conservative staged_label (ARCHIVE > DELETE).
    winner = engine._select_candidate(
        (
            vote("d", StagedLabel.DELETE_CANDIDATE),
            vote("a", StagedLabel.ARCHIVE_CANDIDATE),
        )
    )
    assert winner.staged_label == StagedLabel.ARCHIVE_CANDIDATE


def test_disposition_comes_from_vote_fields_not_rule_name():
    """ephemeral / skip_llm on the winning vote drive the result; the engine
    never inspects rule_name."""
    result = engine.evaluate_message(
        make_view(from_addr="noreply@redditmail.com", subject="Top posts"), self_ctx()
    )
    assert result.ephemeral is True  # from the digest vote's field
    voice = engine.evaluate_message(make_view(labels=frozenset({"sms"})), self_ctx())
    assert voice.classified_by == CLASSIFIED_BY_VOICE  # from the voice vote's skip_llm


def self_ctx() -> RuleContext:
    return RuleContext(known_contacts=frozenset({FRIEND_ADDR}))


def test_rule_categories_are_canonical():
    """Every category a rule can vote must be in models.CATEGORIES (lockstep
    with the LLM's constrained category enum)."""
    from local_llm_email_cleaner.models import CATEGORIES
    from local_llm_email_cleaner.rules import candidate_rules as cr
    from local_llm_email_cleaner.rules import protection_rules as pr

    def view(**overrides) -> MessageView:
        return make_view(**overrides)

    ctx = RuleContext(known_contacts=frozenset({FRIEND_ADDR}))
    # Each entry: a rule fired against an input that makes it vote.
    votes = [
        cr.receipt(view(subject="your receipt"), ctx),
        cr.updates_label(view(labels=frozenset({"category updates"})), ctx),
        cr.digest(view(from_addr="noreply@redditmail.com"), ctx),
        cr.shipping(view(subject="has shipped"), ctx),
        cr.calendar(view(subject="Invitation: party"), ctx),
        cr.spam_label(view(labels=frozenset({"spam"})), ctx),
        cr.promotional_label(view(labels=frozenset({"promotions"})), ctx),
        cr.social_label(view(labels=frozenset({"social"})), ctx),
        cr.noreply_sender(view(from_addr="noreply@x.example"), ctx),
        cr.newsletter_unsubscribe(view(list_unsubscribe=True), ctx),
        pr.known_contact(view(from_addr=FRIEND_ADDR), ctx),
        pr.financial_legal_medical(view(subject="tax documents"), ctx),
        pr.security_alert(view(subject="security alert: new sign-in"), ctx),
    ]
    seen = set()
    for vote in votes:
        assert vote is not None
        seen.add(vote.category)
    assert seen <= set(CATEGORIES), seen - set(CATEGORIES)


def test_every_rule_has_a_rationale():
    """RULE_RATIONALE must cover exactly the live rules — no missing entry (a new
    rule shipped without a rationale) and no stale one (a removed rule)."""
    from local_llm_email_cleaner.rules.candidate_rules import CANDIDATE_RULES
    from local_llm_email_cleaner.rules.protection_rules import PROTECTION_RULES
    from local_llm_email_cleaner.rules.rationale import RULE_RATIONALE

    rule_names = {fn.__name__ for fn in PROTECTION_RULES + CANDIDATE_RULES}
    assert set(RULE_RATIONALE) == rule_names


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


def test_run_rules_stages_voice_messages(conn):
    from conftest import insert_message

    mid = insert_message(
        conn,
        from_addr="+12164969651@unknown.email",
        subject="SMS with Michael",
        labels="SMS",
    )
    engine.run_rules(conn, RuleContext())

    row = conn.execute(
        "SELECT staged_label, proposed_action, ai_category, classified_by "
        "FROM messages WHERE id=?",
        (mid,),
    ).fetchone()
    assert row["staged_label"] == "DELETE_CANDIDATE"
    assert row["proposed_action"] == "trash"
    assert row["ai_category"] == "voice_sms"
    assert row["classified_by"] == CLASSIFIED_BY_VOICE  # so the LLM skips it

    hit = conn.execute(
        "SELECT rule_kind, outcome FROM rule_hits WHERE message_id=? AND rule_name='voice'",
        (mid,),
    ).fetchone()
    assert hit["rule_kind"] == "candidate"
    assert hit["outcome"] == "DELETE_CANDIDATE"


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
