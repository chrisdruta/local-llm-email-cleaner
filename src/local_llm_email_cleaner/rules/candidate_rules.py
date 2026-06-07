"""Candidate rules: vote a message into a cleanup class.

These only stage candidates — nothing is acted on without the policy gate
(LLM confidence + human/auto approval) downstream.
"""

from __future__ import annotations

from ..models import RuleKind, RuleVote, StagedLabel
from . import patterns
from .views import MessageView, RuleContext


def _vote(name: str, label: StagedLabel, category: str) -> RuleVote:
    return RuleVote(
        rule_name=name,
        rule_kind=RuleKind.CANDIDATE,
        staged_label=label,
        category=category,
    )


def receipt(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if patterns.RECEIPT_SUBJECT_RE.search(msg.subject):
        return _vote("receipt", StagedLabel.ARCHIVE_CANDIDATE, "receipt")
    return None


def updates_label(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if msg.labels & (patterns.UPDATES_LABELS | patterns.FORUMS_LABELS):
        return _vote("updates_label", StagedLabel.ARCHIVE_CANDIDATE, "notification")
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


def newsletter_unsubscribe(msg: MessageView, ctx: RuleContext) -> RuleVote | None:
    if msg.list_unsubscribe:
        return _vote(
            "newsletter_unsubscribe", StagedLabel.UNSUBSCRIBE_CANDIDATE, "newsletter"
        )
    return None


CANDIDATE_RULES = (
    receipt,
    updates_label,
    shipping,
    calendar,
    spam_label,
    promotional_label,
    social_label,
    noreply_sender,
    newsletter_unsubscribe,
)
