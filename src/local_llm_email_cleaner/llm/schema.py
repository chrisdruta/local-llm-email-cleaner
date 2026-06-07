"""Structured output schema for the email classifier."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EmailClassification(BaseModel):
    """The JSON object the local model must return for every email."""

    action: Literal["keep", "archive", "trash", "review"] = Field(
        description="What to do with the email. Use 'review' whenever uncertain."
    )
    category: str = Field(
        description=(
            "Short category slug, e.g. promotion, newsletter, social, shipping, "
            "receipt, calendar, personal, security, financial, spam, other"
        )
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in the action, 0.0-1.0"
    )
    reason: str = Field(description="One short sentence explaining the decision")
