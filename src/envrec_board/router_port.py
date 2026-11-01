"""What the board needs from the router, exactly as docs/ROUTER_API.md section 2 defines it (T4a implements it; until it is merged a stand-in with the same shapes is used in tests).

Everything crossing this port is a plain dict: `Task`, `RouteDecision`, `Refusal`, `TaskHandle`, `Intent`. The board never reads a decision's `rule_trace` and never chooses among candidates.
"""

from __future__ import annotations

from typing import Protocol


class RouterPort(Protocol):
    def route(self, task: dict) -> dict: ...  # RouteDecision | Refusal; the Task may carry input_refs and exclude
    def dispatch(self, decision: dict) -> dict: ...  # TaskHandle | Refusal
    def result_of(self, handle: dict) -> dict: ...  # {state, output_ref}: output_ref is a CaptureRef once state == "done"
    def set_grader(self, task_id: str, spec: dict) -> None: ...  # by the task's creator; spec {kind: equals|contains|regex|sha256|run_tests, value}
    def grade(self, task_id: str, output_ref: str) -> dict: ...  # {passed, grader: "code", evidence_ref} | Refusal (no_grader, output_missing, sandbox_missing, weak_grader)
    def report_result(self, task_id: str, escalation_step: int, grader_result: dict) -> dict | None: ...  # RouteDecision | Refusal | None
    def filter_outbound(self, intent: dict, entry_chat: dict) -> dict: ...
    def record_owner_decision(self, parent_task_id: str, subtasks: list[dict], gesture: str) -> str: ...  # [{id, type, sensitivity}]; call only AFTER a human gesture; returns the OWNER event id
    def approve_decomposition(self, parent_task_id: str, subtasks: list[dict], owner_event_id: str) -> str | dict: ...  # decomposition id | Refusal(no_owner_origin): the coordinator re-checks the event
    def own_subtask(self, task_id: str, decomposition_id: str, subtask_id: str) -> str | None: ...  # the OWNER event id the origin rests on; matched by the listed id, or None


def is_refusal(x: object) -> bool:
    return isinstance(x, dict) and "code" in x and "route" not in x


GRADER_KINDS = ("equals", "contains", "regex", "sha256", "run_tests")  # ROUTER_API section 10; `model` and `human` never pass
FATAL_REFUSALS = ("confidential_from_cloud_chat", "sensitivity_missing")  # the task itself is not allowed to exist; the run stops
