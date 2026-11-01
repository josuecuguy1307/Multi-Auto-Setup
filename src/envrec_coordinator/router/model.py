"""Types of the router API (docs/ROUTER_API.md section 1)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SENSITIVITIES = ("public", "internal", "confidential")
SUBTYPES = ("provider", "own_harness", "embedded", "local_model")


class EntryChat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["local", "cloud"]
    name: str = ""


class Exclude(BaseModel):
    """A hard filter the task's creator sets (for example: a verifier must not run on the machine that produced the output). It never relaxes."""
    model_config = ConfigDict(extra="forbid")
    machine_ids: list[str] = Field(default_factory=list, max_length=64)
    environment_ids: list[str] = Field(default_factory=list, max_length=64)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=64)
    task_type: str
    sensitivity: str | None = None
    entry_chat: EntryChat
    created_by: Literal["OWNER", "PLANNER_ACCEPTED"]
    payload_ref: str  # a store reference, or `result-of:<task_id>` (the output of an earlier task, resolved by the coordinator's store)
    input_refs: list[str] = Field(default_factory=list, max_length=16)  # further inputs (same forms); the task's level is the highest of its own and its inputs'
    exclude: Exclude | None = None


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment_id: str
    subtype: str
    model_id: str
    access_id: str
    machine_id: str

    @property
    def key(self) -> str:
        return "|".join((self.environment_id, self.model_id, self.access_id, self.machine_id))


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: Literal["sensitivity", "exclusion", "admission_filter", "cost_class", "admission_rate", "time", "load", "tie_break"]
    candidates_in: list[str]
    candidates_out: list[str]
    values: dict[str, Any] = Field(default_factory=dict)
    margin: float | None = None
    note: str = ""


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    route: Route
    rule_trace: list[Step]
    escalation_step: Literal[0, 1, 2]
    evidence_label: str
    left_out: dict[str, str] = Field(default_factory=dict)  # every environment that was not a candidate on a machine, with the owner-facing reason (so the reasoning is whole: candidates, who was left out and why)
    provisional: bool = False  # chosen with no valid admitted measurement (public and internal tasks only; a confidential task never gets one)
    effective_sensitivity: str = "public"  # the task's own level or the highest level among its inputs, whichever is higher


class Refusal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    code: str
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class GradeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    grader: Literal["code"]
    evidence_ref: str


class TaskHandle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    machine_id: str
    node_task_id: str
    task_state: str
    escalation_step: int
