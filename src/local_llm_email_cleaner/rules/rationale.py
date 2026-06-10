"""Human-readable, one-line rationale per rule — the plain-English version of
each rule's intent, for the review UI's decision trace.

Keyed by rule function name, which equals the ``rule_name`` recorded in
``rule_hits`` (every rule's ``_vote`` passes its own function name). A lockstep
test asserts this dict covers exactly PROTECTION_RULES + CANDIDATE_RULES, so a
new rule can't ship without a rationale. Keep each entry to one short sentence.
"""

from __future__ import annotations

#: rule_name -> why this rule stages a message the way it does.
RULE_RATIONALE: dict[str, str] = {
    # Protection (force KEEP, excluded from auto-approval)
    "known_contact": "Sender is in your derived contacts (you've emailed them) — always kept.",
    "financial_legal_medical": (
        "Subject or body matches financial/legal/medical terms — kept as a "
        "potentially important record."
    ),
    "security_alert": "Looks like an account-security notification — kept.",
    # Candidate (vote a cleanup class; nothing acts without the policy gate)
    "receipt": "Subject looks like a receipt/order confirmation — archived, not trashed.",
    "updates_label": "Carries Gmail's Updates/Forums category — archived.",
    "digest": (
        "Timely, recurring digest/roundup — disposable once its day passes "
        "(may skip the age floor when the LLM agrees)."
    ),
    "shipping": "Shipping/delivery notification — disposable clutter.",
    "calendar": "Calendar invitation or update — disposable once past.",
    "spam_label": "Gmail's own filter already flagged it as Spam.",
    "promotional_label": "Carries Gmail's Promotions category — marketing mail.",
    "social_label": "Carries Gmail's Social category — social-network noise.",
    "noreply_sender": "Sent from a no-reply / automated address.",
    "voice": (
        "Google Voice SMS/call-log/voicemail record — backed up to disk by "
        "voice-export, then staged for trash (the LLM never judges it)."
    ),
    "newsletter_unsubscribe": "Has a List-Unsubscribe header — a newsletter.",
}
