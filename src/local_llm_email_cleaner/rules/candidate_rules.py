"""Candidate rules: vote a message into a cleanup class.

These only stage candidates — nothing is acted on without the policy gate
(LLM confidence + human/auto approval) downstream.
"""

from __future__ import annotations

from ..ingest import voice as voice_ingest
from ..models import RuleKind, RuleVote, StagedLabel
from . import patterns
from .views import MessageView, RuleContext


# Priorities above the default 0 let a vote win outright over the normal
# staged_label precedence: voice (synthetic records, never LLM-judged) beats
# digest (timely/disposable), which beats everything ordinary.
_PRIORITY_DIGEST = 10
_PRIORITY_VOICE = 20


def _vote(
    name: str,
    label: StagedLabel,
    category: str,
    *,
    ephemeral: bool = False,
    skip_llm: bool = False,
    priority: int = 0,
) -> RuleVote:
    return RuleVote(
        rule_name=name,
        rule_kind=RuleKind.CANDIDATE,
        staged_label=label,
        category=category,
        ephemeral=ephemeral,
        skip_llm=skip_llm,
        priority=priority,
    )


def receipt(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if patterns.RECEIPT_SUBJECT_RE.search(msg.subject):
        return _vote("receipt", StagedLabel.ARCHIVE_CANDIDATE, "receipt")
    return None


def updates_label(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if msg.labels & (patterns.UPDATES_LABELS | patterns.FORUMS_LABELS):
        return _vote("updates_label", StagedLabel.ARCHIVE_CANDIDATE, "notification")
    return None


def digest(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    # Timely/recurring digests (daily Reddit, news/social roundups) are
    # disposable once their day passes. The engine treats a digest hit as
    # ephemeral, so the policy gate may auto-trash it without the usual age
    # floor (see rules/engine.py and policy.py).
    sender_hit = msg.from_addr and patterns.DIGEST_SENDER_RE.search(msg.from_addr)
    if sender_hit or patterns.DIGEST_SUBJECT_RE.search(msg.subject):
        # ephemeral: the policy gate may waive the age floor (when the LLM also
        # confirms); priority: beats a co-firing ARCHIVE vote (e.g. Reddit
        # digests also carry the Forums label).
        return _vote(
            "digest",
            StagedLabel.DELETE_CANDIDATE,
            "digest",
            ephemeral=True,
            priority=_PRIORITY_DIGEST,
        )
    return None


def shipping(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if patterns.SHIPPING_SUBJECT_RE.search(msg.subject):
        return _vote("shipping", StagedLabel.DELETE_CANDIDATE, "shipping")
    return None


def calendar(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if patterns.CALENDAR_SUBJECT_RE.search(msg.subject):
        return _vote("calendar", StagedLabel.DELETE_CANDIDATE, "calendar")
    return None


def spam_label(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    # Takeout exports the Spam folder with X-Gmail-Labels: Spam — Gmail's own
    # classifier already condemned these.
    if msg.labels & patterns.SPAM_LABELS:
        return _vote("spam_label", StagedLabel.DELETE_CANDIDATE, "spam")
    return None


def promotional_label(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if msg.labels & patterns.PROMO_LABELS:
        return _vote("promotional_label", StagedLabel.DELETE_CANDIDATE, "promotion")
    return None


def social_label(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if msg.labels & patterns.SOCIAL_LABELS:
        return _vote("social_label", StagedLabel.DELETE_CANDIDATE, "social")
    return None


def noreply_sender(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if msg.from_addr and patterns.NOREPLY_SENDER_RE.search(msg.from_addr):
        return _vote("noreply_sender", StagedLabel.DELETE_CANDIDATE, "automated")
    return None


def voice(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    # Google Voice SMS / call-log / voicemail records (synthetic emails Takeout
    # exports with an SMS / Call log / Voicemail label). `voice-export` backs
    # them up to disk; here we stage them for trash. The engine special-cases
    # this hit so it skips the LLM and never auto-approves — see rules/engine.py.
    kind = voice_ingest.classify_kind(msg.labels)
    if kind is not None:
        # skip_llm: synthetic records the LLM can't meaningfully judge (the
        # export already backed them up); priority: wins over any other label.
        return _vote(
            "voice",
            StagedLabel.DELETE_CANDIDATE,
            f"voice_{kind}",
            skip_llm=True,
            priority=_PRIORITY_VOICE,
        )
    return None


def newsletter_unsubscribe(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if msg.list_unsubscribe:
        return _vote(
            "newsletter_unsubscribe", StagedLabel.UNSUBSCRIBE_CANDIDATE, "newsletter"
        )
    return None


CANDIDATE_RULES = (
    receipt,
    updates_label,
    digest,
    shipping,
    calendar,
    spam_label,
    promotional_label,
    social_label,
    noreply_sender,
    voice,
    newsletter_unsubscribe,
)
