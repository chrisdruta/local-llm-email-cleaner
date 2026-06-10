"""Reconstruct the decision hierarchy for one message, for the review UI.

The raw ``rule_hits`` table and ``ai_reason`` are already shown in the detail
panel; this turns them into a readable narrative — which rule won and why, then
how the LLM second opinion acted. The rules story is produced by RE-RUNNING the
engine (``evaluate_message``) on the row, so precedence is never reimplemented
here and can't drift; the LLM story is read from the stored ``ai_*`` columns.

Because the rules story is re-evaluated against the CURRENT contacts and rule
code, it reflects today's reasoning — if a rule was tuned after the row was
staged, the trace shows why it would be staged now (the useful answer for
debugging), which may differ from the historical ``rule_hits`` snapshot.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..models import (
    CLASSIFIED_BY_LLM,
    CLASSIFIED_BY_RULES,
    CLASSIFIED_BY_VOICE,
    KNOWN_CONTACT_RULE,
    RuleKind,
    RuleVote,
    StagedLabel,
)
from ..rules import engine
from ..rules.rationale import RULE_RATIONALE
from ..rules.views import MessageView


@dataclass(frozen=True)
class DecisionTrace:
    """The narrative pieces for one row's staging decision."""

    rules_lines: list[str]
    rules_label: str
    llm_lines: list[str]  # empty when the LLM was not consulted
    final_label: str | None
    final_action: str | None
    review_status: str

    def to_markdown(self) -> str:
        parts = ["**Rules stage**", *(f"- {line}" for line in self.rules_lines)]
        if self.llm_lines:
            parts += ["", "**LLM stage**", *(f"- {line}" for line in self.llm_lines)]
        parts += [
            "",
            f"**Outcome:** staged **{self.final_label}** · action "
            f"**{self.final_action}** · status **{self.review_status}**",
        ]
        return "\n".join(parts)


def build_decision_trace(conn: sqlite3.Connection, row: sqlite3.Row) -> DecisionTrace:
    """Build the trace for a ``SELECT * FROM messages`` row (as the detail panel
    already fetches)."""
    result = engine.evaluate_message(
        MessageView.from_row(row), engine.load_context(conn)
    )
    rules_label = result.staged_label.value
    return DecisionTrace(
        rules_lines=_rules_narrative(result),
        rules_label=rules_label,
        llm_lines=_llm_narrative(row, rules_label),
        final_label=row["staged_label"],
        final_action=row["proposed_action"],
        review_status=row["review_status"],
    )


def _why(hit: RuleVote) -> str:
    return RULE_RATIONALE.get(hit.rule_name, "")


def _winning_candidate(
    result: engine.RuleResult, candidates: list[RuleVote]
) -> RuleVote:
    """The candidate vote the engine staged from — matched by the label+category
    it produced (engine derives the RuleResult from the winning vote)."""
    for hit in candidates:
        if hit.staged_label == result.staged_label and hit.category == result.category:
            return hit
    return candidates[0]


def _rules_narrative(result: engine.RuleResult) -> list[str]:
    protection = [h for h in result.hits if h.rule_kind == RuleKind.PROTECTION]
    candidates = [h for h in result.hits if h.rule_kind == RuleKind.CANDIDATE]
    lines: list[str] = []

    if result.staged_label == StagedLabel.KEEP:
        decider, *also = protection
        absolute = decider.rule_name == KNOWN_CONTACT_RULE
        tier = "Absolute protection" if absolute else "Overridable protection"
        held = (
            "Never reviewed down."
            if absolute
            else "Not Spam-labeled, so it holds → KEEP."
        )
        lines.append(f"{tier} — **{decider.rule_name}**: {_why(decider)} {held}")
        lines += [f"Also matched **{h.rule_name}**: {_why(h)}" for h in also]
        return lines

    if protection:
        names = ", ".join(f"**{h.rule_name}**" for h in protection)
        lines.append(
            f"Keyword protection matched ({names}) but Gmail's Spam label "
            "suppressed it — recorded so auto-approval stays blocked."
        )
    if candidates:
        winner = _winning_candidate(result, candidates)
        line = f"Candidate **{winner.rule_name}** → {winner.staged_label.value}: {_why(winner)}"
        others = [h.rule_name for h in candidates if h is not winner]
        if others:
            line += (
                f" Won over {', '.join(others)} (highest priority, then the "
                "most-conservative label)."
            )
        lines.append(line)
        if result.ephemeral:
            lines.append(
                "Marked **ephemeral** — disposable once its day passes; the age "
                "floor may be waived when the LLM agrees."
            )
    elif not protection:
        lines.append("No rule matched → **NEEDS_REVIEW** (handed to the LLM).")
    else:
        lines.append(
            "No cleanup-candidate rule matched → **NEEDS_REVIEW** (handed to the LLM)."
        )
    return lines


def _verdict_line(row: sqlite3.Row) -> str:
    conf = row["ai_confidence"]
    conf_s = f"{conf:.2f}" if conf is not None else "—"
    return (
        f"verdict: category **{row['ai_category']}**, confidence {conf_s} — "
        f'"{row["ai_reason"] or "—"}"'
    )


def _llm_narrative(row: sqlite3.Row, rules_label: str) -> list[str]:
    by = row["classified_by"]
    if by is None:
        return ["Not yet classified by the LLM."]
    if by == CLASSIFIED_BY_RULES:
        return ["Decided by rules; not sent to the LLM."]
    if by == CLASSIFIED_BY_VOICE:
        return [
            "Voice-export record — staged for trash; the LLM intentionally skips it."
        ]

    final = row["staged_label"]
    verdict = _verdict_line(row)
    if by == CLASSIFIED_BY_LLM:
        return [f"Rules had no verdict; the LLM classified it → **{final}**.", verdict]

    # rules+llm: a second opinion on a rule-staged row.
    needs_review = StagedLabel.NEEDS_REVIEW.value
    if final == rules_label:
        head = f"LLM confirmed the rules staging (**{final}**)."
    elif final == needs_review and rules_label != needs_review:
        head = (
            f"LLM disagreed with the rules staging (**{rules_label}**) → back to "
            "human review."
        )
    else:
        head = f"LLM second opinion moved it **{rules_label}** → **{final}**."
    lines = [head, verdict]
    if rules_label == StagedLabel.KEEP.value and final != StagedLabel.KEEP.value:
        lines.append(
            "Protection hit retained — can't auto-approve; a human still confirms."
        )
    return lines
