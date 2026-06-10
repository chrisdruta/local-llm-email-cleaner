"""rules.toml loading, validation, and matching semantics."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from local_llm_email_cleaner.rules.matcher import CompiledRule, compile_ruleset
from local_llm_email_cleaner.rules.ruleset import (
    RulesConfigError,
    RuleSet,
    load_ruleset,
)
from local_llm_email_cleaner.rules.views import MessageView, RuleContext


def make_ruleset(tmp_path: Path, toml_text: str) -> RuleSet:
    path = tmp_path / "rules.toml"
    path.write_text(toml_text, encoding="utf-8")
    return load_ruleset(path)


def load_errors(tmp_path: Path, toml_text: str) -> RulesConfigError:
    with pytest.raises(RulesConfigError) as excinfo:
        make_ruleset(tmp_path, toml_text)
    return excinfo.value


def view(**overrides) -> MessageView:
    defaults = dict(
        id=1,
        from_addr="sender@example.com",
        from_name="Sender",
        subject="Hello",
        labels=frozenset(),
        has_attachments=False,
        list_unsubscribe=False,
        body_text="",
    )
    defaults.update(overrides)
    return MessageView(**defaults)


CTX = RuleContext(known_contacts=frozenset({"friend@example.com"}))


def one_rule(tmp_path: Path, body: str) -> CompiledRule:
    ruleset = make_ruleset(tmp_path, f'[[rules]]\nname = "r"\naction = "trash"\n{body}')
    (compiled,) = compile_ruleset(ruleset)
    return compiled


# --- loading & defaults -------------------------------------------------------


def test_default_rules_template_loads_and_compiles(tmp_path):
    text = (
        resources.files("local_llm_email_cleaner")
        .joinpath("rules/default_rules.toml")
        .read_text(encoding="utf-8")
    )
    ruleset = make_ruleset(tmp_path, text)
    names = [r.name for r in ruleset.rules]
    assert names[0] == "voice"
    assert "known_contact" in names and "promotional_label" in names
    compiled = compile_ruleset(ruleset)
    assert [c.name for c in compiled][:2] == ["voice", "known_contact"]
    protect = ruleset.rule("known_contact")
    assert protect.protect and protect.action == "keep" and not protect.confirm_with_llm
    digest = ruleset.rule("digest")
    assert digest.ephemeral and digest.confirm_with_llm and digest.action == "trash"


def test_rule_defaults(tmp_path):
    ruleset = make_ruleset(
        tmp_path,
        """
        [[rules]]
        name = "r"
        action = "trash"
        [[rules.match]]
        from_domain = ["Example.COM"]
        """,
    )
    (rule,) = ruleset.rules
    assert rule.priority == 100
    assert rule.enabled and not rule.protect and not rule.confirm_with_llm
    assert not rule.ephemeral and rule.category is None
    assert rule.match[0].from_domain == ("example.com",)  # lowercased


def test_missing_file_and_bad_toml(tmp_path):
    with pytest.raises(RulesConfigError, match="not found"):
        load_ruleset(tmp_path / "nope.toml")
    err = load_errors(tmp_path, "[[rules]\nname=")
    assert "invalid TOML" in str(err)


# --- validation errors (all collected, attributed to rules) -------------------


def test_validation_collects_all_errors(tmp_path):
    err = load_errors(
        tmp_path,
        """
        [[rules]]
        name = "bad_regex"
        action = "trash"
        [[rules.match]]
        subject_regex = '(unclosed'

        [[rules]]
        name = "no_action"
        [[rules.match]]
        from_domain = ["a.com"]
        """,
    )
    messages = [str(e) for e in err.errors]
    assert any("bad_regex" in m and "invalid regex" in m for m in messages)
    assert any("no_action" in m and "action is required" in m for m in messages)


def test_duplicate_names_rejected(tmp_path):
    err = load_errors(
        tmp_path,
        """
        [[rules]]
        name = "dup"
        action = "trash"
        [[rules.match]]
        from_domain = ["a.com"]

        [[rules]]
        name = "dup"
        action = "keep"
        [[rules.match]]
        from_domain = ["b.com"]
        """,
    )
    assert any("duplicate rule name" in str(e) for e in err.errors)


@pytest.mark.parametrize(
    ("snippet", "needle"),
    [
        # protect contradicts confirm_with_llm
        (
            '[[rules]]\nname="r"\nprotect=true\nconfirm_with_llm=true\n'
            "[[rules.match]]\nknown_contact=true\n",
            "contradict",
        ),
        # protect forces keep
        (
            '[[rules]]\nname="r"\nprotect=true\naction="trash"\n'
            "[[rules.match]]\nknown_contact=true\n",
            "must have action = 'keep'",
        ),
        # empty match block
        (
            '[[rules]]\nname="r"\naction="trash"\n[[rules.match]]\n',
            "at least one criterion",
        ),
        # no match blocks at all
        ('[[rules]]\nname="r"\naction="trash"\n', "match: Field required"),
        # unknown key (extra=forbid)
        (
            '[[rules]]\nname="r"\naction="trash"\nbogus=1\n'
            '[[rules.match]]\nfrom_domain=["a.com"]\n',
            "bogus",
        ),
        # unknown match criterion
        (
            '[[rules]]\nname="r"\naction="trash"\n'
            '[[rules.match]]\nsender_domain=["a.com"]\n',
            "sender_domain",
        ),
        # bad action value
        (
            '[[rules]]\nname="r"\naction="delete"\n'
            '[[rules.match]]\nfrom_domain=["a.com"]\n',
            "action",
        ),
    ],
)
def test_validation_rejects(tmp_path, snippet, needle):
    err = load_errors(tmp_path, snippet)
    assert needle in str(err)


def test_protect_implies_keep_when_action_omitted(tmp_path):
    ruleset = make_ruleset(
        tmp_path,
        '[[rules]]\nname="p"\nprotect=true\n[[rules.match]]\nknown_contact=true\n',
    )
    assert ruleset.rules[0].action == "keep"


# --- ordering ------------------------------------------------------------------


def test_priority_then_file_order(tmp_path):
    ruleset = make_ruleset(
        tmp_path,
        """
        [[rules]]
        name = "low"
        priority = 10
        action = "trash"
        [[rules.match]]
        from_domain = ["a.com"]

        [[rules]]
        name = "tie_first"
        priority = 50
        action = "archive"
        [[rules.match]]
        from_domain = ["a.com"]

        [[rules]]
        name = "tie_second"
        priority = 50
        action = "trash"
        [[rules.match]]
        from_domain = ["a.com"]

        [[rules]]
        name = "disabled_top"
        priority = 999
        enabled = false
        action = "trash"
        [[rules.match]]
        from_domain = ["a.com"]
        """,
    )
    assert [r.name for r in ruleset.ordered_rules()] == [
        "tie_first",
        "tie_second",
        "low",
    ]


# --- matching semantics ---------------------------------------------------------


def test_criteria_and_within_block(tmp_path):
    rule = one_rule(
        tmp_path,
        '[[rules.match]]\nfrom_domain = ["example.com"]\nsubject_regex = "hello"\n',
    )
    assert rule.matches(view(subject="Hello there"), CTX)
    assert not rule.matches(view(subject="Goodbye"), CTX)
    assert not rule.matches(view(from_addr="x@other.com", subject="Hello"), CTX)


def test_blocks_or_together(tmp_path):
    rule = one_rule(
        tmp_path,
        '[[rules.match]]\nfrom_domain = ["a.com"]\n'
        '[[rules.match]]\nsubject_regex = "weekly digest"\n',
    )
    assert rule.matches(view(from_addr="x@a.com"), CTX)
    assert rule.matches(view(subject="Your Weekly Digest"), CTX)
    assert not rule.matches(view(), CTX)


def test_from_addr_exact_lowercased(tmp_path):
    rule = one_rule(tmp_path, '[[rules.match]]\nfrom_addr = ["Sales@Shop.com"]\n')
    assert rule.matches(view(from_addr="sales@shop.com"), CTX)
    assert not rule.matches(view(from_addr="other@shop.com"), CTX)
    assert not rule.matches(view(from_addr=None), CTX)


def test_from_addr_regex(tmp_path):
    rule = one_rule(tmp_path, "[[rules.match]]\nfrom_addr_regex = 'no-?reply'\n")
    assert rule.matches(view(from_addr="noreply@x.com"), CTX)
    assert rule.matches(view(from_addr="no-reply@x.com"), CTX)
    assert not rule.matches(view(from_addr="hello@x.com"), CTX)
    assert not rule.matches(view(from_addr=None), CTX)


def test_from_domain(tmp_path):
    rule = one_rule(tmp_path, '[[rules.match]]\nfrom_domain = ["redditmail.com"]\n')
    assert rule.matches(view(from_addr="bot@RedditMail.com"), CTX)
    assert not rule.matches(view(from_addr="bot@mail.redditmail.com.evil.com"), CTX)
    assert not rule.matches(view(from_addr="no-at-sign"), CTX)


def test_subject_and_body_regex(tmp_path):
    rule = one_rule(tmp_path, "[[rules.match]]\nbody_regex = 'lab\\s+result'\n")
    assert rule.matches(view(body_text="Your Lab Result is ready"), CTX)
    assert not rule.matches(view(body_text=""), CTX)

    rule = one_rule(tmp_path, "[[rules.match]]\nsubject_regex = '^invitation:'\n")
    assert rule.matches(view(subject="Invitation: standup"), CTX)
    assert not rule.matches(view(subject="Fwd: Invitation: standup"), CTX)


def test_gmail_labels_any_of(tmp_path):
    rule = one_rule(
        tmp_path,
        '[[rules.match]]\ngmail_labels = ["category promotions", "promotions"]\n',
    )
    assert rule.matches(view(labels=frozenset({"inbox", "category promotions"})), CTX)
    assert not rule.matches(view(labels=frozenset({"inbox"})), CTX)


def test_boolean_criteria(tmp_path):
    rule = one_rule(tmp_path, "[[rules.match]]\nlist_unsubscribe = true\n")
    assert rule.matches(view(list_unsubscribe=True), CTX)
    assert not rule.matches(view(), CTX)

    rule = one_rule(tmp_path, "[[rules.match]]\nhas_attachments = false\n")
    assert rule.matches(view(), CTX)
    assert not rule.matches(view(has_attachments=True), CTX)


def test_known_contact_criterion(tmp_path):
    rule = one_rule(tmp_path, "[[rules.match]]\nknown_contact = true\n")
    assert rule.matches(view(from_addr="friend@example.com"), CTX)
    assert not rule.matches(view(from_addr="stranger@example.com"), CTX)

    rule = one_rule(tmp_path, "[[rules.match]]\nknown_contact = false\n")
    assert rule.matches(view(from_addr="stranger@example.com"), CTX)
    assert not rule.matches(view(from_addr="friend@example.com"), CTX)


def test_default_template_voice_outranks_known_contact(tmp_path):
    text = (
        resources.files("local_llm_email_cleaner")
        .joinpath("rules/default_rules.toml")
        .read_text(encoding="utf-8")
    )
    compiled = compile_ruleset(make_ruleset(tmp_path, text))
    sms = view(
        from_addr="friend@example.com",  # leaked into contacts
        labels=frozenset({"sms"}),
        subject="SMS with Friend",
    )
    matches = [c.name for c in compiled if c.matches(sms, CTX)]
    assert matches[0] == "voice"  # evaluation order = priority order
    assert "known_contact" in matches
