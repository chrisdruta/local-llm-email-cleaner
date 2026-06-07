"""Prompt templates for the classifier chain, with classification guardrails."""

from __future__ import annotations

import sqlite3

from langchain_core.prompts import ChatPromptTemplate

SYSTEM_PROMPT = """\
You are classifying email for cleanup of an old Gmail mailbox.

Possible actions:
- keep: the user should keep this email
- archive: worth keeping out of sight, but not deleting
- trash: safe to move to trash
- review: a human must look at it

Never trash:
- legal, tax, medical, financial, or account security email
- personal correspondence
- receipts for expensive items
- active subscriptions or login/security alerts
- anything you are uncertain about — choose "review" instead

Distinguish topic from substance: junk and scam mail routinely *mentions*
legal, tax, payment, or security matters as bait. The protections above are
for genuine mail about the user's own affairs, not for promotions or scams
dressed in those words. If "Gmail labels" includes Spam, Gmail's own filter
flagged the message — treat that as a strong signal toward trash.

Calibrate confidence honestly: 0.95+ only for unmistakable junk
(old promotions, expired offers, stale social notifications, obvious scams)."""

USER_TEMPLATE = """\
Classify this email:

From: {from_line}
Subject: {subject}
Date: {date}
Gmail labels: {labels}
Body excerpt:
{body_excerpt}"""

CLASSIFY_PROMPT = ChatPromptTemplate.from_messages(
    [("system", SYSTEM_PROMPT), ("human", USER_TEMPLATE)]
)


def build_inputs(row: sqlite3.Row, max_body_chars: int) -> dict[str, str]:
    """Map a messages-table row to the prompt variables."""
    from_line = row["from_addr"] or "(unknown)"
    if row["from_name"]:
        from_line = f"{row['from_name']} <{from_line}>"
    body = row["body_text"] or ""
    if len(body) > max_body_chars:
        body = body[:max_body_chars] + "\n[... truncated]"
    return {
        "from_line": from_line,
        "subject": row["subject"] or "(no subject)",
        "date": row["date_utc"] or "(unknown)",
        "labels": row["labels"] or "(none)",
        "body_excerpt": body or "(empty body)",
    }
