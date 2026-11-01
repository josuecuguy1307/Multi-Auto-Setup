"""The coordinator's code graders (docs/ROUTER_API.md section 10). A grader is registered for a task by whoever created it; `grade` applies it to a stored output and returns a verdict that `report_result`
accepts. Kinds that only compare text (`equals`, `contains`, `regex`, `sha256`) run in the coordinator. A kind that EXECUTES model-written code (`run_tests`) runs only through a sandbox runner when a sandbox
backend is ready, else it is refused (`sandbox_missing`): this module never executes anything itself. A model-graded kind never passes (`weak_grader`)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

TEXT_KINDS = ("equals", "contains", "regex", "sha256")
EXECUTING_KINDS = ("run_tests",)
WEAK_KINDS = ("model", "human")
MAX_PATTERN = 500


def check_text(spec: dict[str, Any], text: str) -> bool:
    kind, value = spec["kind"], spec["value"]  # a grader spec is {kind, value}
    if kind == "equals":
        return text.strip() == str(value).strip()
    if kind == "contains":
        return str(value) in text
    if kind == "sha256":
        return hashlib.sha256(text.encode()).hexdigest() == str(value).lower()
    pattern = str(value)
    if len(pattern) > MAX_PATTERN:
        raise ValueError("pattern too long")
    return re.search(pattern, text, re.DOTALL) is not None


def evidence_ref(task_id: str, output_ref: str, spec: dict[str, Any], passed: bool, extra: str = "") -> str:
    body = {"task": task_id, "output": output_ref, "grader": spec.get("kind"), "passed": passed, "extra": extra}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SandboxGone(RuntimeError):
    """The sandbox runner found no backend ready when it was about to start (it went away after `sandbox_ready` answered). Nothing ran; the grade is refused as `sandbox_missing`."""


SandboxRunner = Callable[[Any, dict], dict]  # (output record, spec) -> {"passed": bool, "log_sha256": str}; supplied by the coordinator, runs inside the sandbox backend only
