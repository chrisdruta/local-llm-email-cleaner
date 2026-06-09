"""Structured output schema for the email classifier."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field

from ..models import CATEGORIES, ProposedAction


class EmailClassification(BaseModel):
    """The JSON object the local model must return for every email."""

    action: Literal["keep", "archive", "trash", "review"] = Field(
        description="What to do with the email. Use 'review' whenever uncertain."
    )
    category: Literal[*CATEGORIES] = Field(  # type: ignore[valid-type]
        description=(
            "Short category slug for the email. Use 'digest' for timely/recurring "
            "roundups (daily Reddit digests, news/social notification roundups)."
        )
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in the action, 0.0-1.0"
    )
    ephemeral: bool = Field(
        default=False,
        description=(
            "True only for a timely/recurring digest or roundup with no lasting "
            "value once its day passes — daily Reddit digests, social/news "
            "notification roundups, 'N new posts for you'. Such mail is safe to "
            "trash regardless of how recent it is. False for anything with "
            "lasting reference value (receipts, personal mail, account notices)."
        ),
    )
    reason: str = Field(description="One short sentence explaining the decision")


# The action Literal must stay in lockstep with ProposedAction (and with
# models.LABEL_FOR_LLM_ACTION, which models.py asserts against the enum).
assert set(get_args(EmailClassification.model_fields["action"].annotation)) == {
    a.value for a in ProposedAction
}, "EmailClassification.action Literal must match ProposedAction values"
