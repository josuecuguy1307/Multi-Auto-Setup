"""The `setup_plan`: what the owner approves before anything on a node is written, and the hash that binds the approval to exactly that plan (docs/NETWORK_CONTRACT.md 3.9).

A plan is data. It lists numbered steps; each step says what it is for (`title`), the closed operation a recipe defines (`op` and its `args`), the exact command shown to the owner, the paths it
writes and how to undo it. The plan's hash covers everything that decides what happens and nothing that changes while it runs (`status`, `verification`), so an approval given to a hash cannot
be used for a different plan: a changed step, path, command or recipe makes another hash. The node recomputes the hash from the plan it received; it never trusts the one the plan carries.

The shape is shared with the owner's screens (they render it); extend it, never break it."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

WRITE_ROOT = "<mas>"  # every path a plan writes starts here; the per-OS layer says where it is on this computer (the user's own profile, never a system folder)
_SEG = re.compile(r"^[A-Za-z0-9._@+=-]{1,100}$")
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)  # device names Windows opens instead of a file, with or without an extension
STEP_KINDS = ("download", "install", "configure", "start", "verify")
STEP_STATUS = ("pending", "approved", "running", "done", "failed", "undone")
SENSITIVITY = ("public", "internal", "confidential")
MAX_PLAN_BYTES = 64 * 1024


class PlanError(ValueError):
    """The plan is not a well-formed plan. The message is the typed reason."""


def clean_write_path(p: str) -> str:
    """A declared write path: `<mas>/` then plain segments; one trailing `/` says the path is a folder. No absolute path, no `..`, no backslash, no empty segment, no segment that Windows would treat as a device
    (`con`, `nul`, `com1` ...) or whose trailing dot it would drop: the same text means the same place on every operating system, and a plan valid on one is valid on all."""
    folder = p.endswith("/")
    parts = (p[:-1] if folder else p).split("/")
    if len(parts) < 2 or parts[0] != WRITE_ROOT or any(not s or s in (".", "..") or not _SEG.match(s) or s.endswith(".") or _WINDOWS_RESERVED.match(s) for s in parts[1:]):
        raise PlanError(f"bad_write_path: {p!r} is not `{WRITE_ROOT}/` followed by plain segments")
    return "/".join(parts) + ("/" if folder else "")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fit(_Strict):
    model_bytes: int = Field(ge=0)
    ram_available_bytes: int = Field(ge=0)
    fits: bool
    limited_by: str = ""


class Verification(_Strict):
    endpoint: str = ""  # 127.0.0.1:<port>: the inference server of a node is only ever on loopback (N5)
    ok: bool = False
    latency_ms: int = Field(default=0, ge=0)
    evidence: str = "untested"


class SetupStep(_Strict):
    n: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["download", "install", "configure", "start", "verify"]
    op: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")  # a closed operation of the recipe (the node has a function for it or it refuses)
    args: dict[str, Any] = Field(default_factory=dict)
    exact_command: str = Field(min_length=1, max_length=2000)  # what the owner reads: the operation and its arguments, in words and commands
    writes: list[str] = Field(default_factory=list)
    bytes: int = Field(default=0, ge=0)
    undo: str = Field(min_length=1, max_length=2000)
    needs_owner_approval: bool = True
    status: Literal["pending", "approved", "running", "done", "failed", "undone"] = "pending"

    @field_validator("writes")
    @classmethod
    def _writes(cls, v: list[str]) -> list[str]:
        return [clean_write_path(p) for p in v]


class SetupPlan(_Strict):
    plan_id: str = Field(min_length=1, max_length=80)
    plan_hash: str = ""
    machine_id: str = Field(min_length=1, max_length=80)
    option_id: str = Field(min_length=1, max_length=200)
    sensitivity: Literal["public", "internal", "confidential"]
    recipe_id: str = Field(min_length=1, max_length=80)  # the closed recipe this plan was made from (data in a skill)
    recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # that recipe's own hash: a node whose copy differs refuses
    fit: Fit
    steps: list[SetupStep] = Field(min_length=1, max_length=40)
    verification: Verification = Field(default_factory=Verification)

    @field_validator("steps")
    @classmethod
    def _numbered(cls, v: list[SetupStep]) -> list[SetupStep]:
        if [s.n for s in v] != list(range(1, len(v) + 1)):
            raise PlanError("bad_plan: steps are numbered 1, 2, 3 ... in order")
        return v

    def step(self, n: int) -> SetupStep:
        for s in self.steps:
            if s.n == n:
                return s
        raise PlanError(f"bad_plan: there is no step {n}")


def parse_plan(raw: Any) -> SetupPlan:
    if not isinstance(raw, dict) or len(json.dumps(raw, default=str)) > MAX_PLAN_BYTES:
        raise PlanError("bad_plan: not a plan, or too large")
    try:
        return SetupPlan.model_validate(raw)
    except PlanError:
        raise
    except Exception as exc:
        raise PlanError(f"bad_plan: {str(exc)[:160]}") from None


def canonical_plan(plan: SetupPlan) -> bytes:
    """What the hash covers: every field that decides what happens. Not `plan_hash` itself, nor what changes while the plan runs (each step's `status`, the `verification` result)."""
    d = plan.model_dump(mode="json", exclude={"plan_hash", "verification"})
    for s in d["steps"]:
        s.pop("status", None)
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def plan_hash(plan: SetupPlan) -> str:
    return "sha256:" + hashlib.sha256(canonical_plan(plan)).hexdigest()


def seal(plan: SetupPlan) -> SetupPlan:
    """The plan with its own hash filled in (what the plan builder returns)."""
    return plan.model_copy(update={"plan_hash": plan_hash(plan)})


def recipe_sha256(recipe: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
