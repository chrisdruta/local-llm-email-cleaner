"""Pattern data for the rules engine. Tune lists here, not the rule logic."""

from __future__ import annotations

import re

# --- sender patterns -------------------------------------------------------

NOREPLY_SENDER_RE = re.compile(
    r"(?:^|[.\-_@])(?:no[\-_.]?reply|do[\-_.]?not[\-_.]?reply|donotreply|mailer-daemon)",
    re.IGNORECASE,
)

SECURITY_SENDER_RE = re.compile(
    r"^(?:security|account-security|no-reply@accounts)", re.IGNORECASE
)

# Known senders of timely, recurring digests/roundups — no lasting value once
# the day passes. Matched against the full from_addr. Add domains here.
DIGEST_SENDER_RE = re.compile(
    r"@(?:redditmail\.com|e?mail\.nextdoor\.com|.*\.substack\.com)$",
    re.IGNORECASE,
)

# --- Gmail label sets (lowercased; Takeout uses e.g. "Category Promotions") --

PROMO_LABELS = {"category promotions", "promotions"}
SPAM_LABELS = {"spam"}
SOCIAL_LABELS = {"category social", "social"}
UPDATES_LABELS = {"category updates", "updates"}
FORUMS_LABELS = {"category forums", "forums"}

# --- subject patterns: cleanup candidates -----------------------------------

# Note: no bare "your order" — that would swallow shipping notifications.
RECEIPT_SUBJECT_RE = re.compile(
    r"\b(?:receipt|order\s+confirmation|payment\s+(?:received|confirmation)|"
    r"invoice\s+#?\d)",
    re.IGNORECASE,
)

SHIPPING_SUBJECT_RE = re.compile(
    r"\b(?:has\s+shipped|shipping\s+confirmation|out\s+for\s+delivery|was\s+delivered|"
    r"tracking\s+number|on\s+its\s+way|delivery\s+update)",
    re.IGNORECASE,
)

CALENDAR_SUBJECT_RE = re.compile(
    r"^(?:invitation:|accepted:|declined:|tentative:|updated\s+invitation:|"
    r"canceled(?:\s+event)?:|reminder:)",
    re.IGNORECASE,
)

# Timely digests/roundups by subject shape (sender-agnostic backstop).
DIGEST_SUBJECT_RE = re.compile(
    r"\b(?:daily|weekly|monthly)\s+(?:digest|roundup|recap|briefing|newsletter)\b|"
    r"\bdigest\b|\btop\s+(?:posts|stories)\b|"
    r"\b\d+\s+new\s+(?:posts|stories|notifications|messages|replies)\b|"
    r"\bwhat'?s\s+(?:new|happening|trending)\b|\bhighlights?\s+(?:for\s+you|this\s+week)\b",
    re.IGNORECASE,
)

# --- subject patterns: protection (never auto-acted) ------------------------

SECURITY_SUBJECT_RE = re.compile(
    r"\b(?:security\s+alert|sign[\-\s]?in\s+(?:attempt|detected|from)|new\s+device|"
    r"password\s+(?:was\s+)?(?:changed|reset)|reset\s+your\s+password|"
    r"verification\s+code|one[\-\s]?time\s+(?:pass(?:word|code)?|pin)|\botp\b|"
    r"two[\-\s]?factor|\b2fa\b|login\s+attempt|suspicious\s+activity)",
    re.IGNORECASE,
)

FINANCIAL_LEGAL_MEDICAL_SUBJECT_RE = re.compile(
    r"\b(?:tax(?:es)?\b|\birs\b|w-?2\b|1099|account\s+statement|bank\s+statement|"
    r"insurance|policy\s+(?:number|renewal|document)|claim\s+(?:status|number)|"
    r"legal|lawyer|attorney|contract|lease|deed|notary|"
    r"medical|doctor|prescription|pharmacy|lab\s+result|diagnosis|appointment\s+(?:confirmed|reminder)|"
    r"mortgage|loan|payroll|salary|benefits|401\s?k|\bhsa\b|\bfsa\b)",
    re.IGNORECASE,
)
