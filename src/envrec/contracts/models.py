"""The shapes every module speaks. Pydantic v2, extra=forbid.

Invariants live here, not in callers:
- a fact with no evidence cannot pass `hypothesis`
- `verified` needs the verifier or two independent providers
- `contradicted` keeps at least two evidence entries
- an `unknown` fact has no value
- keys must use the fixed namespace
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from envrec.contracts.base import Strict, new_id, utcnow, word_count
from envrec.contracts.enums import (
    ActionType,
    Actor,
    CreatedBy,
    Decision,
    FactStatus,
    HypothesisOutcome,
    ModelKind,
    Origin,
    RingDecision,
    RingPolicy,
    TargetKind,
    Tier,
)
from envrec.contracts.keys import check_key

SHA256_PATTERN = r"^[0-9a-f]{64}$"
ONE_LINE_MAX_WORDS = 12
WHY_MAX_WORDS = 30


class RawRef(Strict):
    """Pointer to a raw output file. The content never travels inline."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)


class Evidence(Strict):
    provider: str = Field(min_length=1, max_length=64)
    probe: str = Field(min_length=1, max_length=96)
    event_id: str | None = None
    raw_ref: RawRef | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=240)


class Target(Strict):
    id: str = Field(default_factory=lambda: new_id("tgt"))
    kind: TargetKind
    ring: int = Field(ge=0, le=3)
    handle: str = Field(min_length=1, max_length=200)
    # Set only by the human-written config file. No Action can carry this field.
    authorized: bool = False
    parent: str | None = None
    signature: str | None = None


class Fact(Strict):
    id: str = Field(default_factory=lambda: new_id("fact"))
    target_id: str
    key: str
    value: JsonValue = None
    status: FactStatus = FactStatus.unknown
    evidence: list[Evidence] = Field(default_factory=list)
    supersedes: str | None = None
    created_by: CreatedBy = CreatedBy.probe
    ts: datetime = Field(default_factory=utcnow)
    # Values claimed by disagreeing sources when status is contradicted.
    alt_values: list[JsonValue] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _key_in_namespace(cls, key: str) -> str:
        return check_key(key)

    @model_validator(mode="after")
    def _status_rules(self) -> Fact:
        s = self.status
        if s in (FactStatus.tested, FactStatus.verified, FactStatus.contradicted) and not self.evidence:
            raise ValueError(
                f"status {s.value!r} needs evidence; a fact without evidence cannot pass 'hypothesis'"
            )
        if s == FactStatus.verified and self.created_by != CreatedBy.verifier:
            providers = {e.provider for e in self.evidence}
            if len(providers) < 2:
                raise ValueError(
                    "status 'verified' needs the verifier or two independent providers; "
                    f"got providers {sorted(providers)}"
                )
        if s == FactStatus.contradicted and len(self.evidence) < 2:
            raise ValueError("status 'contradicted' must keep both evidence entries (need at least 2)")
        if s == FactStatus.unknown and self.value is not None:
            raise ValueError("an 'unknown' fact has no value")
        return self


class ToolSpec(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$", max_length=60)  # a tool, or `skill.<id>`
    ring: int = Field(ge=0, le=3)
    tier: Tier
    target_kinds: list[TargetKind] = Field(min_length=1)
    input_model: dict[str, JsonValue] = Field(default_factory=dict)  # JSON schema of the args
    one_line: str
    description: str = Field(default="", max_length=1024)  # a skill's full "Use when" description: what the menu shows for it
    cost: Literal["cheap", "medium", "expensive"] = "cheap"
    origin: Origin = Origin.builtin
    rung: int | None = Field(default=None, ge=1, le=6)
    source: str = Field(min_length=1)
    # Generated tools stay unverified (and blocked on real targets) until a verifier passes them.
    verified: bool = False
    # Words the policy denylist looks for: delete, format, flash, elevate.
    tags: list[str] = Field(default_factory=list)
    # Fact keys this tool writes. The menu hides a tool once all of its keys have a fact.
    produces: list[str] = Field(default_factory=list)
    # Fact keys that must already exist (known or tried) before the tool is worth offering.
    requires: list[str] = Field(default_factory=list)

    @field_validator("one_line")
    @classmethod
    def _short_description(cls, text: str) -> str:
        n = word_count(text)
        if n == 0 or n > ONE_LINE_MAX_WORDS:
            raise ValueError(f"one_line must be 1 to {ONE_LINE_MAX_WORDS} words, got {n}")
        return text


class Coverage(Strict):
    scope: str = ""
    status: Literal["complete", "partial", "unknown"]
    checked: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)


class Unknown(Strict):
    key: str
    question: str = Field(default="", max_length=200)

    @field_validator("key")
    @classmethod
    def _key_in_namespace(cls, key: str) -> str:
        return check_key(key)


class ProbeResult(Strict):
    ok: bool
    facts: list[Fact] = Field(default_factory=list)
    raw_ref: RawRef | None = None
    error: str | None = Field(default=None, max_length=300)
    coverage: Coverage | None = None
    unknowns: list[Unknown] = Field(default_factory=list)

    @model_validator(mode="after")
    def _failure_explains_itself(self) -> ProbeResult:
        if not self.ok and not self.error:
            raise ValueError("a failed ProbeResult (ok=false) must carry an error")
        return self


class Action(Strict):
    """The only thing a model may output. Flat on purpose: small models fill flat forms."""

    action: ActionType
    tool: str | None = None
    args: dict[str, JsonValue] = Field(default_factory=dict)
    target_id: str | None = None
    ring: int | None = Field(default=None, ge=0, le=3)
    fact_id: str | None = None
    key: str | None = None
    question: str | None = Field(default=None, max_length=300)
    skill_draft: dict[str, JsonValue] | None = None  # propose_skill: the draft of a skill (see the authoring skill)
    to: str | None = Field(default=None, max_length=40)  # advance: the protocol step to move to
    why: str = ""

    @field_validator("why")
    @classmethod
    def _why_is_short(cls, text: str) -> str:
        n = word_count(text)
        if n > WHY_MAX_WORDS:
            raise ValueError(f"why must be at most {WHY_MAX_WORDS} words, got {n}")
        return text

    @field_validator("key")
    @classmethod
    def _key_in_namespace(cls, key: str | None) -> str | None:
        return None if key is None else check_key(key)

    @model_validator(mode="after")
    def _required_fields(self) -> Action:
        a = self.action
        missing: list[str] = []
        if a == ActionType.call_tool and not self.tool:
            missing.append("tool")
        elif a == ActionType.extend_to and self.ring is None:
            missing.append("ring")
        elif a == ActionType.expand_fact and not (self.fact_id or self.key):
            missing.append("fact_id or key")
        elif a == ActionType.advance and not self.to:
            missing.append("to")
        elif a in (ActionType.research, ActionType.ask_human) and not self.question:
            missing.append("question")
        elif a == ActionType.propose_skill and not self.skill_draft:
            missing.append("skill_draft")
        if missing:
            raise ValueError(f"action {a.value!r} requires: {', '.join(missing)}")
        return self


class PolicyDecision(Strict):
    decision: Decision
    reason: str = Field(min_length=1)
    tool: str
    target_id: str | None = None


class Event(Strict):
    id: str = Field(default_factory=lambda: new_id("evt"))
    ts: datetime = Field(default_factory=utcnow)
    actor: Actor
    type: str = Field(min_length=1, max_length=64)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    run_id: str | None = None


class Hypothesis(Strict):
    id: str = Field(default_factory=lambda: new_id("hyp"))
    target_id: str
    claim: str = Field(min_length=1, max_length=300)
    rung: int = Field(ge=1, le=6)
    based_on: list[str] = Field(default_factory=list)  # fact ids
    test: str = ""
    expected: str = ""
    outcome: HypothesisOutcome = HypothesisOutcome.pending
    fact_id: str | None = None
    # Where the guess came from (a database or signature set); used for per-source stats.
    source: str = ""
    # Confirming reads tried so far. The ladder allows ONE harmless confirming read per guess.
    attempts: int = Field(default=0, ge=0)


class Skill(Strict):
    id: str = Field(default_factory=lambda: new_id("skill"))
    signature: str = Field(min_length=1)
    code_ref: str
    tests_ref: str
    evidence: list[Evidence] = Field(default_factory=list)
    verified_runs: int = Field(default=0, ge=0)


class RingCoverage(Strict):
    ring: int = Field(ge=0, le=3)
    checked: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    decision: RingDecision

    def to_fact(self, target_id: str, evidence: Evidence | None = None) -> Fact:
        """Stored as fact `ring.<n>.coverage`."""
        ev = evidence or Evidence(provider="loop", probe="ring_coverage", confidence=1.0)
        return Fact(
            target_id=target_id,
            key=f"ring.{self.ring}.coverage",
            value={
                "checked": list(self.checked),
                "unknown": list(self.unknown),
                "decision": self.decision.value,
            },
            status=FactStatus.tested,
            evidence=[ev],
            created_by=CreatedBy.loop,
        )


class ModelProfile(Strict):
    """The only dial that differs between a small and a strong model is view_budget_facts."""

    name: str = Field(min_length=1)
    kind: ModelKind = ModelKind.openai_compat
    model_id: str = Field(min_length=1)  # always from config, never hard-coded
    endpoint: str = Field(min_length=1)
    api_key_env: str | None = None  # NAME of an environment variable, never the key
    supports_tool_calling: bool = False
    context_tokens: int = Field(gt=0)
    view_budget_facts: int = Field(ge=1, le=200)
    menu_max: int = Field(ge=2, le=12)
    ring_policy: RingPolicy = RingPolicy.fixed_order
