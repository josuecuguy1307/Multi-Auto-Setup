"""The coordinator's store of task outputs (docs/ROUTER_API.md section 10). An output is addressed by a CaptureRef (the SHA-256 of its canonical content) and carries the sensitivity of the task that produced it: a
result inherits the level of its source, so a confidential result is never handed to a cloud environment by being passed along as an input. `MemoryOutputs` is the in-process form used by tests and by the
SIMULATED-LOCAL runs; the coordinator's persistent store implements the same three methods."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class OutputRecord:
    ref: str
    task_id: str
    sensitivity: str
    text: str
    files: list[dict[str, Any]] = field(default_factory=list)


class Outputs(Protocol):
    def put(self, task_id: str, sensitivity: str, result: dict) -> str: ...
    def get(self, ref: str) -> OutputRecord | None: ...
    def of_task(self, task_id: str) -> OutputRecord | None: ...


def capture_ref(task_id: str, content: dict) -> str:
    return hashlib.sha256(json.dumps({"task": task_id, "content": content}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class MemoryOutputs:
    def __init__(self) -> None:
        self._by_ref: dict[str, OutputRecord] = {}
        self._latest: dict[str, str] = {}

    def put(self, task_id: str, sensitivity: str, result: dict) -> str:
        content = {"text": str(result.get("output_text", "")), "files": list(result.get("files", []))}
        ref = capture_ref(task_id, content)
        self._by_ref[ref] = OutputRecord(ref, task_id, sensitivity, content["text"], content["files"])
        self._latest[task_id] = ref
        return ref

    def get(self, ref: str) -> OutputRecord | None:
        return self._by_ref.get(ref)

    def of_task(self, task_id: str) -> OutputRecord | None:
        ref = self._latest.get(task_id)
        return self._by_ref.get(ref) if ref else None
