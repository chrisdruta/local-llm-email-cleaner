"""rules.toml loading and validation.

The rules file is the user-tunable source of truth for deterministic staging:
each rule declares match criteria and an action (keep/archive/trash), plus
flags controlling how it interacts with the LLM and the policy gates:

- ``protect = true``     — absolute keep: decided by rules alone, never sent to
                           the LLM, and any keep-voting hit blocks auto-approval
                           downstream regardless of which rule won.
- ``confirm_with_llm``   — the rule's verdict is tentative; the LLM classifies
                           the message independently and a disagreement routes
                           it to human review.
- ``priority``           — highest wins; ties broken by file order.
- ``ephemeral = true``   — marks timely/disposable mail (digests); the
                           auto-trash gate may waive its age floor when the LLM
                           agrees the message is ephemeral.

Validation collects *every* problem in the file (not just the first) so a
tuning session gets one complete report — see :class:`RulesConfigError`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

#: actions a rule may stage. "review" is not offered: a rule that can't decide
#: should simply not match (no-rule-match already routes to the LLM).
RULE_ACTIONS = ("keep", "archive", "trash")


class MatchSpec(BaseModel):
    """One match block. Criteria within a block AND together; a rule's blocks
    OR together. Regexes are case-insensitive; list values are lowercased."""

    model_config = ConfigDict(extra="forbid")

    from_addr: tuple[str, ...] | None = None
    from_addr_regex: str | None = None
    from_domain: tuple[str, ...] | None = None
    subject_regex: str | None = None
    body_regex: str | None = None
    gmail_labels: tuple[str, ...] | None = None
    list_unsubscribe: bool | None = None
    has_attachments: bool | None = None
    known_contact: bool | None = None

    @field_validator("from_addr", "from_domain", "gmail_labels")
    @classmethod
    def _lowercase(cls, v: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if v is None:
            return None
        cleaned = tuple(item.strip().lower() for item in v if item.strip())
        if not cleaned:
            raise ValueError("list criterion must contain at least one value")
        return cleaned

    @field_validator("from_addr_regex", "subject_regex", "body_regex")
    @classmethod
    def _compilable(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from None
        return v

    @model_validator(mode="after")
    def _at_least_one_criterion(self) -> MatchSpec:
        if all(getattr(self, name) is None for name in type(self).model_fields):
            raise ValueError("match block must set at least one criterion")
        return self


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    #: highest priority wins; ties broken by file order
    priority: int = 100
    #: implied "keep" for protect rules; required otherwise
    action: Literal["keep", "archive", "trash"] | None = None
    #: absolute keep — never LLM-checked, blocks auto-approval downstream
    protect: bool = False
    #: verdict is tentative; LLM double-checks, disagreement → human review
    confirm_with_llm: bool = False
    #: timely/disposable (digests) — gate may waive the age floor if LLM agrees
    ephemeral: bool = False
    category: str | None = None
    enabled: bool = True
    match: tuple[MatchSpec, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _protect_implies_keep(cls, data: dict) -> dict:
        if isinstance(data, dict) and data.get("protect") and "action" not in data:
            data["action"] = "keep"
        return data

    @model_validator(mode="after")
    def _consistent_flags(self) -> Rule:
        if self.protect and self.action != "keep":
            raise ValueError("protect rules must have action = 'keep'")
        if self.protect and self.confirm_with_llm:
            raise ValueError(
                "protect and confirm_with_llm contradict: protect rules are "
                "absolute and never LLM-checked"
            )
        if self.action is None:
            raise ValueError("action is required (keep, archive, or trash)")
        return self


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    rules: tuple[Rule, ...] = ()

    def ordered_rules(self) -> tuple[Rule, ...]:
        """Enabled rules in evaluation order: priority desc, ties by file order
        (Python's stable sort preserves it)."""
        enabled = [r for r in self.rules if r.enabled]
        return tuple(sorted(enabled, key=lambda r: -r.priority))

    def rule(self, name: str) -> Rule | None:
        return next((r for r in self.rules if r.name == name), None)


@dataclass(frozen=True)
class RuleError:
    """One validation problem, attributed to a rule when possible."""

    rule: str | None  # rule name, or None for file-level problems
    field: str  # dotted path within the rule (e.g. "match[1].subject_regex")
    message: str

    def __str__(self) -> str:
        where = f"rule '{self.rule}': " if self.rule else ""
        return f"{where}{self.field}: {self.message}" if self.field else (
            f"{where}{self.message}"
        )


class RulesConfigError(Exception):
    """All validation problems in a rules file, collected in one pass."""

    def __init__(self, path: Path, errors: list[RuleError]):
        self.path = path
        self.errors = errors
        lines = "\n".join(f"  - {err}" for err in errors)
        super().__init__(f"{path}: {len(errors)} problem(s) in rules file:\n{lines}")


def _rule_label(raw: dict, loc: tuple) -> tuple[str | None, str]:
    """Map a pydantic error loc to (rule name, field path within the rule)."""
    if not loc or loc[0] != "rules" or len(loc) < 2 or not isinstance(loc[1], int):
        return None, ".".join(str(part) for part in loc)
    index = loc[1]
    rules = raw.get("rules", [])
    name = None
    if isinstance(rules, list) and index < len(rules) and isinstance(rules[index], dict):
        name = rules[index].get("name") or f"#{index + 1}"
    else:
        name = f"#{index + 1}"
    field = ""
    rest = loc[2:]
    for part in rest:
        if isinstance(part, int):
            field += f"[{part}]"
        else:
            field = f"{field}.{part}" if field else str(part)
    return name, field


def write_default_rules(path: Path) -> None:
    """Materialize the packaged starter rules.toml (used by `init`)."""
    template = (
        resources.files("local_llm_email_cleaner")
        .joinpath("rules/default_rules.toml")
        .read_text(encoding="utf-8")
    )
    path.write_text(template, encoding="utf-8")


def load_ruleset(path: Path) -> RuleSet:
    """Parse and validate a rules.toml. Raises :class:`RulesConfigError` with
    every problem found, or returns the validated :class:`RuleSet`."""
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise RulesConfigError(
            path, [RuleError(None, "", "rules file not found — run `email-cleaner init`")]
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise RulesConfigError(path, [RuleError(None, "", f"invalid TOML: {exc}")]) from None

    errors: list[RuleError] = []
    ruleset: RuleSet | None = None
    try:
        ruleset = RuleSet.model_validate(raw)
    except ValidationError as exc:
        for err in exc.errors():
            rule_name, field = _rule_label(raw, err["loc"])
            errors.append(RuleError(rule_name, field, err["msg"]))

    if ruleset is not None:
        seen: set[str] = set()
        for rule in ruleset.rules:
            if rule.name in seen:
                errors.append(RuleError(rule.name, "name", "duplicate rule name"))
            seen.add(rule.name)

    if errors:
        raise RulesConfigError(path, errors)
    assert ruleset is not None
    return ruleset
