"""The harness sidecar of a skill (`skill.meta.json`): everything the standard SKILL.md frontmatter has no room for.

The folder stays a valid standard skill that other agents can read; this file only adds what this harness enforces: where the skill applies (signature), who made it
(provenance), what it collects (the plan: registered tools only), how far it was verified, and, for a protocol, the states the loop enforces."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from envrec.contracts.base import Strict

PLATFORMS = {"macos", "linux", "windows", "android", "any"}


class PlanStep(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    tool: str
    platforms: list[str] = Field(default_factory=lambda: ["any"])
    args: dict[str, Any] = Field(default_factory=dict)  # values may be `{param.<name>}`
    optional: bool = False  # a failing optional step does not fail the skill
    reducer: str | None = None  # a privacy reducer for this step's capture (the gate knows the closed list)

    @field_validator("platforms")
    @classmethod
    def _p(cls, v: list[str]) -> list[str]:
        if not v or not set(v) <= PLATFORMS:
            raise ValueError(f"platforms must be within {sorted(PLATFORMS)}")
        return v


class Plan(Strict):
    """What a skill collects, and the script that turns the captures into facts. The script never runs anything: it may only ASK for further registered-tool calls
    (`next`), and the harness decides whether they run."""

    steps: list[PlanStep] = Field(default_factory=list)
    script: str | None = "scripts/parse.py"
    max_rounds: int = Field(default=4, ge=1, le=6)
    timeout_s: float = Field(default=10.0, gt=0, le=30)


class Signature(Strict):
    kind: str | None = None
    signature: str | None = None
    facts: list[dict[str, Any]] = Field(default_factory=list)  # [{key, equals | regex | exists}]


class Advance(Strict):
    to: str
    needs: list[dict[str, Any]] = Field(default_factory=list)  # evidence required to leave the state (see protocols.machine)


class State(Strict):
    goal: str = ""
    tags: list[str] = Field(default_factory=list)  # tool tags offered in this state
    tools: list[str] = Field(default_factory=list)  # extra tool or skill names offered in this state
    actions: list[str] = Field(default_factory=list)  # extra model actions allowed here (research, propose_skill, ...)
    skills: list[str] = Field(default_factory=list)  # guidance skills whose body stays in the view for every turn of this step (the authoring skill while a skill is being written)
    advance: list[Advance] = Field(default_factory=list)
    terminal: bool = False
    reason: str = ""
    timeout_s: float | None = Field(default=None, gt=0)  # a protocol that code walks (pairing): seconds this step may last; past it the guard moves to `on_timeout`
    on_timeout: str | None = None


class ProtocolDef(Strict):
    kinds: list[str] = Field(min_length=1)
    start: str
    states: dict[str, State]

    @model_validator(mode="after")
    def _consistent(self) -> ProtocolDef:
        if self.start not in self.states:
            raise ValueError(f"the start state {self.start!r} is not defined")
        for name, st in self.states.items():
            for a in st.advance:
                if a.to not in self.states:
                    raise ValueError(f"state {name!r} advances to {a.to!r}, which is not defined")
            if (st.timeout_s is None) != (st.on_timeout is None) or (st.on_timeout is not None and st.on_timeout not in self.states):
                raise ValueError(f"state {name!r}: timeout_s and on_timeout go together, and on_timeout must be a defined state")
        if not any(s.terminal for s in self.states.values()):
            raise ValueError("a protocol needs at least one terminal state")
        return self


class SkillMeta(Strict):
    provenance: str = "builtin"
    protocol: str = "generic"  # the protocol this skill belongs to
    tier: str = "observe"
    ring: int = Field(default=0, ge=0, le=3)
    platforms: list[str] = Field(default_factory=lambda: ["any"])
    signature: Signature = Field(default_factory=Signature)
    plan: Plan | None = None
    states: ProtocolDef | None = None
    produces: list[str] = Field(default_factory=list)  # fact keys this skill can write (the menu hides a skill whose keys are all known)
    verified_runs: int = Field(default=0, ge=0)
    example: bool = False
    identify: bool = False  # runs at the first rung to produce the facts a signature can match (a banner, a vendor id), before any model call
    legacy_id: str | None = None  # the id of the YAML skill or adapter it replaced (golden outputs are keyed by it)
    baseline: dict[str, Any] | None = None  # generated skills: the with/without-skill comparison that justified accepting it
    script_sha256: str | None = None  # generated skills: pinned hash of the script that was approved
    recipe: dict[str, Any] | None = None  # the write steps of a workspace self-setup as data (envrec.recipes); the owner approves its canonical hash together with the script

    @field_validator("provenance")
    @classmethod
    def _prov(cls, v: str) -> str:
        if v not in ("builtin", "generated"):
            raise ValueError("provenance is builtin or generated")
        return v

    @field_validator("tier")
    @classmethod
    def _tier(cls, v: str) -> str:
        if v not in ("observe", "read"):
            raise ValueError("tier is observe or read")
        return v

    @field_validator("platforms")
    @classmethod
    def _p(cls, v: list[str]) -> list[str]:
        if not v or not set(v) <= PLATFORMS:
            raise ValueError(f"platforms must be within {sorted(PLATFORMS)}")
        return v
