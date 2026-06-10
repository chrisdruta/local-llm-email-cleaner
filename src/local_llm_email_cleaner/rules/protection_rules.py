"""Protection rules: run first, force KEEP, exclude from auto-approval.

A protected message is never sent to the LLM and never enters the policy
gates. One amendment: Gmail's own Spam label overrides the *keyword* rules
for staging (scam subjects imitate exactly those topics), but never the
known-contact rule — and overridden keyword hits are still recorded, which
keeps such messages out of the auto-approval gates (human review only).
"""

from __future__ import annotations

from ..models import KNOWN_CONTACT_RULE, RuleKind, RuleVote, StagedLabel
from . import patterns
from .views import MessageView, RuleContext


def _vote(name: str, category: str) -> RuleVote:
    return RuleVote(
        rule_name=name,
        rule_kind=RuleKind.PROTECTION,
        staged_label=StagedLabel.KEEP,
        category=category,
    )


def known_contact(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if ctx.is_known_contact(msg.from_addr):
        return _vote(KNOWN_CONTACT_RULE, "personal")
    return None


# Protection scans BOTH subject and body: sensitive mail (a bank statement, a
# lab result) often carries an innocuous subject with the substance only in the
# body. The body is matched against a STRICTER pattern than the subject (see
# patterns.py) — bare keywords like "legal" or "reset your password" live in the
# footer of nearly every promo, so the body needs an unambiguous multi-word
# phrase. Keyword KEEPs are still double-checked by the LLM in `classify`.
def financial_legal_medical(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if patterns.FINANCIAL_LEGAL_MEDICAL_RE.search(msg.subject) or (
        msg.body_text and patterns.FINANCIAL_LEGAL_MEDICAL_BODY_RE.search(msg.body_text)
    ):
        return _vote("financial_legal_medical", "financial_legal_medical")
    return None


def security_alert(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if patterns.SECURITY_RE.search(msg.subject) or (
        msg.body_text and patterns.SECURITY_BODY_RE.search(msg.body_text)
    ):
        return _vote("security_alert", "security")
    if msg.from_addr and patterns.SECURITY_SENDER_RE.search(msg.from_addr):
        return _vote("security_alert", "security")
    return None


ABSOLUTE_PROTECTION_RULES = (known_contact,)
OVERRIDABLE_PROTECTION_RULES = (financial_legal_medical, security_alert)
PROTECTION_RULES = ABSOLUTE_PROTECTION_RULES + OVERRIDABLE_PROTECTION_RULES
