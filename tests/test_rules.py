"""Rules engine: priority wins, hits recorded, LLM-confirm rows left open."""

from __future__ import annotations

from conftest import FRIEND_ADDR, insert_message, make_ruleset

from local_llm_email_cleaner.ingest import contacts, store
from local_llm_email_cleaner.rules import engine
from local_llm_email_cleaner.rules.matcher import compile_ruleset
from local_llm_email_cleaner.rules.views import MessageView, RuleContext

CTX = RuleContext(known_contacts=frozenset({FRIEND_ADDR}))


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


class TestEvaluateAgainstDefaultRules:
    """The packaged default ruleset reproduces the v2 staging behavior."""

    import pytest

    @pytest.fixture(autouse=True)
    def _compile(self, default_ruleset):
        self.compiled = compile_ruleset(default_ruleset)

    def winner(self, view: MessageView):
        hits = engine.evaluate_message(view, CTX, self.compiled)
        return hits[0].rule if hits else None

    def hit_names(self, view: MessageView) -> set[str]:
        return {h.name for h in engine.evaluate_message(view, CTX, self.compiled)}

    def test_known_contact_protected(self):
        winner = self.winner(make_view(from_addr=FRIEND_ADDR))
        assert winner.name == "known_contact"
        assert winner.protect and winner.action == "keep"

    def test_financial_subject_keep(self):
        winner = self.winner(make_view(subject="Your 2023 tax documents are ready"))
        assert winner.name == "financial_legal_medical"
        assert winner.action == "keep" and winner.confirm_with_llm

    def test_security_alert_keep(self):
        winner = self.winner(
            make_view(subject="Security alert: new sign-in from Windows")
        )
        assert winner.name == "security_alert"

    def test_financial_body_despite_innocuous_subject(self):
        winner = self.winner(
            make_view(
                from_addr="noreply@bank.example",
                subject="Your monthly update is ready",
                body_text="Your bank statement for account ending 1234 is available.",
            )
        )
        assert winner.name == "financial_legal_medical"

    def test_security_body_despite_innocuous_subject(self):
        winner = self.winner(
            make_view(
                from_addr="x@service.example",
                subject="A note for you",
                body_text="Your verification code is 558213. Do not share it.",
            )
        )
        assert winner.name == "security_alert"

    def test_financial_body_ignores_promo_footer_keywords(self):
        # Bare footer words ("legal", "insurance", a lone "tax", "benefits")
        # must not trigger the strict body pattern; nothing else matches either,
        # so the message goes to the LLM.
        winner = self.winner(
            make_view(
                from_addr="deals@wayfair.example",
                subject="MEMORIAL DAY CLEARANCE",
                body_text=(
                    "Shop our best deals! 0% financing available. See our legal "
                    "terms. Tax-free weekend. Member benefits apply. Insurance "
                    "not included."
                ),
            )
        )
        assert winner is None

    def test_security_body_ignores_reset_password_cta(self):
        winner = self.winner(
            make_view(
                from_addr="hello@alphalete.example",
                subject="A tee you buy twice",
                body_text=(
                    "New drop is live! Manage your account or reset your "
                    "password any time from your profile."
                ),
            )
        )
        assert winner is None

    def test_security_body_still_matches_password_change_notice(self):
        winner = self.winner(
            make_view(
                subject="A note for you",
                body_text="Your password was changed on a new device.",
            )
        )
        assert winner.name == "security_alert"

    def test_known_contact_beats_promo_label(self):
        winner = self.winner(
            make_view(from_addr=FRIEND_ADDR, labels=frozenset({"category promotions"}))
        )
        assert winner.name == "known_contact"

    def test_spam_outranks_keyword_keep_but_keep_hit_recorded(self):
        # Legal-bait subject on Gmail-flagged spam: spam (300) outranks the
        # keyword keep (250), but the keep-voting hit stays recorded so the
        # policy gates can never auto-approve it.
        view = make_view(subject="Final legal notice", labels=frozenset({"spam"}))
        assert self.winner(view).name == "spam_label"
        assert {"financial_legal_medical", "spam_label"} <= self.hit_names(view)

    def test_spam_never_outranks_known_contact(self):
        winner = self.winner(
            make_view(from_addr=FRIEND_ADDR, labels=frozenset({"spam"}))
        )
        assert winner.name == "known_contact"

    def test_promo_label_votes_trash(self):
        winner = self.winner(make_view(labels=frozenset({"category promotions"})))
        assert winner.name == "promotional_label"
        assert winner.action == "trash" and winner.confirm_with_llm

    def test_receipt_outranks_promo_label(self):
        # Archive rules sit above trash rules: the conservative outcome wins.
        winner = self.winner(
            make_view(
                subject="Receipt for your purchase",
                labels=frozenset({"category promotions"}),
            )
        )
        assert winner.name == "receipt" and winner.action == "archive"

    def test_list_unsubscribe_alone_goes_to_llm(self):
        # newsletter_unsubscribe ships commented out in the template.
        assert self.winner(make_view(list_unsubscribe=True)) is None

    def test_no_hits_goes_to_llm(self):
        hits = engine.evaluate_message(make_view(), CTX, self.compiled)
        assert hits == ()

    def test_digest_outranks_updates_label_and_is_ephemeral(self):
        winner = self.winner(
            make_view(
                from_addr="noreply@redditmail.com",
                subject="Top posts from your communities",
                labels=frozenset({"category forums"}),
                list_unsubscribe=True,
            )
        )
        assert winner.name == "digest"
        assert winner.action == "trash" and winner.ephemeral

    def test_digest_subject_backstop(self):
        winner = self.winner(
            make_view(from_addr="news@somesite.example", subject="Your daily digest")
        )
        assert winner.name == "digest"

    def test_known_contact_outranks_digest(self):
        winner = self.winner(
            make_view(from_addr=FRIEND_ADDR, subject="Your weekly digest")
        )
        assert winner.name == "known_contact"

    def test_voice_sms_decides_alone(self):
        winner = self.winner(make_view(labels=frozenset({"sms"})))
        assert winner.name == "voice"
        assert winner.action == "trash" and not winner.confirm_with_llm

    def test_voice_outranks_known_contact(self):
        # A leaked phone-number contact must not shield Voice records.
        winner = self.winner(
            make_view(from_addr=FRIEND_ADDR, labels=frozenset({"sms"}))
        )
        assert winner.name == "voice"

    def test_voice_outranks_other_labels(self):
        winner = self.winner(make_view(labels=frozenset({"sms", "category updates"})))
        assert winner.name == "voice"


# --- run_rules persistence -----------------------------------------------------


def test_run_rules_end_to_end(conn, mbox_path, cfg, default_ruleset):
    store.ingest_mbox(conn, mbox_path)
    contacts.derive_contacts(conn, cfg.user_addresses)
    ctx = engine.load_context(conn)

    counts = engine.run_rules(conn, default_ruleset, ctx)
    assert sum(counts.values()) == 7
    # Only the known-contact keep decides alone; every matched candidate wants
    # LLM confirmation and the unmatched rows go to the LLM outright.
    assert counts["keep"] == 1
    assert counts["needs_llm"] == 6

    def row(message_id: str):
        return conn.execute(
            "SELECT rule_name, rule_action, rule_protected, action, decision_source "
            "FROM messages WHERE rfc_message_id=?",
            (message_id,),
        ).fetchone()

    friend = row("friend-1@example.com")
    assert friend["rule_name"] == "known_contact"
    assert friend["action"] == "keep" and friend["decision_source"] == "rule"
    assert friend["rule_protected"] == 1

    bank = row("bank-1@example.com")  # keyword keep, awaiting LLM confirmation
    assert bank["rule_name"] == "financial_legal_medical"
    assert bank["rule_action"] == "keep" and bank["action"] is None

    promo = row("promo-1@example.com")
    assert promo["rule_name"] == "promotional_label"
    assert promo["rule_action"] == "trash" and promo["action"] is None

    ship = row("ship-1@example.com")  # shipping outranks noreply by file order
    assert ship["rule_name"] == "shipping"

    photos = row("photos-1@example.com")  # no rule matched -> LLM
    assert photos["rule_name"] is None and photos["action"] is None

    # All matches recorded; the winner flagged.
    hits = conn.execute(
        """
        SELECT rule_hits.rule_name, won FROM rule_hits
        JOIN messages ON messages.id = rule_hits.message_id
        WHERE messages.rfc_message_id = 'ship-1@example.com'
        """
    ).fetchall()
    assert {(h["rule_name"], h["won"]) for h in hits} == {
        ("shipping", 1),
        ("noreply_sender", 0),
    }

    # Second run is a no-op (only un-ruled rows are evaluated).
    assert sum(engine.run_rules(conn, default_ruleset, ctx).values()) == 0


def test_run_rules_decides_voice_alone(conn, default_ruleset):
    mid = insert_message(
        conn,
        from_addr="+12164969651@unknown.email",
        subject="SMS with Michael",
        labels="SMS",
    )
    engine.run_rules(conn, default_ruleset, RuleContext())

    row = conn.execute(
        "SELECT rule_name, rule_action, action, decision_source, llm_action "
        "FROM messages WHERE id=?",
        (mid,),
    ).fetchone()
    assert row["rule_name"] == "voice"
    assert row["action"] == "trash" and row["decision_source"] == "rule"
    assert row["llm_action"] is None  # never sent to the LLM


def test_reset_preserves_non_pending_rule_hits(conn, mbox_path, default_ruleset):
    """rules --reset must not orphan approved/applied rows from their hits."""
    store.ingest_mbox(conn, mbox_path)
    ctx = engine.load_context(conn)
    engine.run_rules(conn, default_ruleset, ctx)

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

    engine.run_rules(conn, default_ruleset, ctx, reset=True)

    assert hits(approved_id) > 0
    assert hits(pending_id) > 0
    assert (
        conn.execute(
            "SELECT rule_name FROM messages WHERE id=?", (approved_id,)
        ).fetchone()[0]
        is not None
    )


def test_reset_preserves_llm_verdicts_and_refinalizes(conn, tmp_path, default_ruleset):
    """Tuning rules.toml + re-running never re-pays LLM time: stored verdicts
    are kept and the final action is re-derived from them."""
    mid = insert_message(
        conn, from_addr="deals@shop.example", labels="Category Promotions"
    )
    ctx = RuleContext()
    engine.run_rules(conn, default_ruleset, ctx)
    # Simulate the classifier having confirmed the promo as trash.
    conn.execute(
        "UPDATE messages SET llm_action='trash', llm_category='promotion', "
        "llm_confidence=0.97, llm_reason='obvious promo', action='trash', "
        "decision_source='rule+llm' WHERE id=?",
        (mid,),
    )
    conn.commit()

    # Re-run after a "tuning pass" (same rules here): the row re-finalizes from
    # the stored verdict without going back to the LLM.
    engine.run_rules(conn, default_ruleset, ctx, reset=True)
    row = conn.execute(
        "SELECT action, decision_source, llm_confidence FROM messages WHERE id=?",
        (mid,),
    ).fetchone()
    assert row["action"] == "trash" and row["decision_source"] == "rule+llm"
    assert row["llm_confidence"] == 0.97

    # A tuning pass that flips the rule to archive now DISAGREES with the
    # stored trash verdict -> review, still no LLM re-run.
    flipped = make_ruleset(
        tmp_path,
        """
        [[rules]]
        name = "promos_archive_now"
        action = "archive"
        confirm_with_llm = true
        [[rules.match]]
        gmail_labels = ["category promotions"]
        """,
    )
    engine.run_rules(conn, flipped, ctx, reset=True)
    row = conn.execute(
        "SELECT rule_action, action, decision_source FROM messages WHERE id=?", (mid,)
    ).fetchone()
    assert row["rule_action"] == "archive"
    assert row["action"] == "review" and row["decision_source"] == "rule+llm"

    # --reset --full wipes the verdicts too: back to awaiting the LLM.
    engine.run_rules(conn, flipped, ctx, reset=True, full=True)
    row = conn.execute(
        "SELECT llm_action, action FROM messages WHERE id=?", (mid,)
    ).fetchone()
    assert row["llm_action"] is None and row["action"] is None


def test_finalize_with_stored_llm_no_rule_uses_llm_action(conn):
    mid = insert_message(
        conn,
        ruled_at="2026-01-01T00:00:00",
        llm_action="archive",
        llm_confidence=0.8,
    )
    assert engine.finalize_with_stored_llm(conn) == 1
    row = conn.execute(
        "SELECT action, decision_source FROM messages WHERE id=?", (mid,)
    ).fetchone()
    assert row["action"] == "archive" and row["decision_source"] == "llm"


def test_run_rules_commits_per_chunk(conn, mbox_path, monkeypatch, default_ruleset):
    store.ingest_mbox(conn, mbox_path)
    ctx = engine.load_context(conn)
    monkeypatch.setattr(engine, "BATCH_SIZE", 2)
    counts = engine.run_rules(conn, default_ruleset, ctx)
    assert sum(counts.values()) == 7
    ruled_rows = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE ruled_at IS NOT NULL"
    ).fetchone()[0]
    assert ruled_rows == 7
