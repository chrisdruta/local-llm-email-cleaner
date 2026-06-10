"""Compile validated rules into predicates over MessageView.

Compilation happens once per run (regexes pre-compiled, lists frozen); the
per-message hot path is plain attribute checks. Criteria within a match block
AND together; a rule's blocks OR together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ruleset import MatchSpec, Rule, RuleSet
from .views import MessageView, RuleContext


def _domain_of(addr: str | None) -> str | None:
    if not addr or "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1].lower()


@dataclass(frozen=True)
class CompiledMatch:
    from_addr: frozenset[str] | None
    from_addr_regex: re.Pattern | None
    from_domain: frozenset[str] | None
    subject_regex: re.Pattern | None
    body_regex: re.Pattern | None
    gmail_labels: frozenset[str] | None
    list_unsubscribe: bool | None
    has_attachments: bool | None
    known_contact: bool | None

    @classmethod
    def compile(cls, spec: MatchSpec) -> CompiledMatch:
        def rx(pattern: str | None) -> re.Pattern | None:
            return re.compile(pattern, re.IGNORECASE) if pattern else None

        return cls(
            from_addr=frozenset(spec.from_addr) if spec.from_addr else None,
            from_addr_regex=rx(spec.from_addr_regex),
            from_domain=frozenset(spec.from_domain) if spec.from_domain else None,
            subject_regex=rx(spec.subject_regex),
            body_regex=rx(spec.body_regex),
            gmail_labels=frozenset(spec.gmail_labels) if spec.gmail_labels else None,
            list_unsubscribe=spec.list_unsubscribe,
            has_attachments=spec.has_attachments,
            known_contact=spec.known_contact,
        )

    def matches(self, msg: MessageView, ctx: RuleContext) -> bool:
        if self.from_addr is not None:
            if not msg.from_addr or msg.from_addr.lower() not in self.from_addr:
                return False
        if self.from_addr_regex is not None:
            if not msg.from_addr or not self.from_addr_regex.search(msg.from_addr):
                return False
        if self.from_domain is not None:
            if _domain_of(msg.from_addr) not in self.from_domain:
                return False
        if self.subject_regex is not None:
            if not self.subject_regex.search(msg.subject):
                return False
        if self.body_regex is not None:
            if not msg.body_text or not self.body_regex.search(msg.body_text):
                return False
        if self.gmail_labels is not None:
            if not (msg.labels & self.gmail_labels):
                return False
        if self.list_unsubscribe is not None:
            if msg.list_unsubscribe != self.list_unsubscribe:
                return False
        if self.has_attachments is not None:
            if msg.has_attachments != self.has_attachments:
                return False
        if self.known_contact is not None:
            if ctx.is_known_contact(msg.from_addr) != self.known_contact:
                return False
        return True


@dataclass(frozen=True)
class CompiledRule:
    rule: Rule
    blocks: tuple[CompiledMatch, ...]

    @property
    def name(self) -> str:
        return self.rule.name

    def matches(self, msg: MessageView, ctx: RuleContext) -> bool:
        return any(block.matches(msg, ctx) for block in self.blocks)


def compile_ruleset(ruleset: RuleSet) -> tuple[CompiledRule, ...]:
    """Enabled rules compiled in evaluation order (priority desc, file order)."""
    return tuple(
        CompiledRule(rule=r, blocks=tuple(CompiledMatch.compile(s) for s in r.match))
        for r in ruleset.ordered_rules()
    )
