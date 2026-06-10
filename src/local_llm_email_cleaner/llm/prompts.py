"""Prompt templates for the classifier chain, with classification guardrails."""

from __future__ import annotations

import sqlite3

from langchain_core.prompts import ChatPromptTemplate

SYSTEM_PROMPT = """\
You are classifying email for cleanup of an old Gmail mailbox.

Possible actions:
- keep: still worth having
- archive: worth keeping out of sight, but not deleting
- trash: safe to move to trash
- review: a human must look at it

Judge each message on its LASTING VALUE to the user, not its topic:
- Age: each email is shown with its date and today's date. The older a
  routine, promotional, or transactional message, the safer it is to trash.
- Continued usefulness: records that still matter — current statements,
  contracts, tax/legal/medical documents, receipts that are tax-relevant,
  warranty-eligible, or for expensive purchases, anything tied to an active
  account — keep or archive. The same kinds of mail with no remaining value
  (an expired card offer, a superseded balance alert, a years-old routine
  receipt, a stale sign-in notification) are trash, financial-sounding or not.
- Sentiment: personal correspondence and anything a person might want to
  reread — keep.
- Routine, low-value transactional mail is disposable clutter — rideshare and
  food-delivery receipts, coffee/retail e-receipts, small everyday order
  confirmations, shipping updates for long-delivered packages.
- Uncertainty: choose "review" whenever you are genuinely unsure.

Timely/recurring digests are an exception to the age guidance: a daily Reddit
digest, a news/social notification roundup, a "N new posts for you" email, or
any periodic digest is worthless once its day passes — choose "trash" and set
"ephemeral": true for these REGARDLESS of how recent they are. Set
"ephemeral": false for everything else (it defaults to false). Do NOT mark
genuine receipts, personal mail, or account/security notices as ephemeral.

Distinguish topic from substance: junk and scam mail routinely *mentions*
legal, tax, payment, or security matters as bait — words alone earn no
protection. If "Gmail labels" includes Spam, Gmail's own filter flagged the
message — treat that as a strong signal toward trash.

Calibrate confidence honestly: 0.95+ only for unmistakable junk (old
promotions, expired offers, stale social notifications, obvious scams, routine
low-value receipts)."""

USER_TEMPLATE = """\
Classify this email (today's date is {today}):

From: {from_line}
Subject: {subject}
Date: {date}
Gmail labels: {labels}
Body excerpt:
{body_excerpt}"""

CLASSIFY_PROMPT = ChatPromptTemplate.from_messages(
    [("system", SYSTEM_PROMPT), ("human", USER_TEMPLATE)]
)


def build_inputs(row: sqlite3.Row, max_body_chars: int, today: str) -> dict[str, str]:
    """Map a messages-table row to the prompt variables.

    `today` (ISO date) is passed in so the LLM can judge a message's age; the
    caller supplies it once per run rather than reading the clock per row.
    """
    from_line = row["from_addr"] or "(unknown)"
    if row["from_name"]:
        from_line = f"{row['from_name']} <{from_line}>"
    body = row["body_text"] or ""
    if len(body) > max_body_chars:
        body = body[:max_body_chars] + "\n[... truncated]"
    return {
        "today": today,
        "from_line": from_line,
        "subject": row["subject"] or "(no subject)",
        "date": row["date_utc"] or "(unknown)",
        "labels": row["labels"] or "(none)",
        "body_excerpt": body or "(empty body)",
    }
