"""Suites: loaded from data, validated, hashed. A suite is the project's own list of tasks for one task type; the hash covers every field that can change a result."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

CODE_GRADERS = frozenset({"sandbox_tests", "exact", "answer_line", "json", "lines"})  # closed set: each is a program deciding pass or fail
WEAK_GRADERS = frozenset({"model", "human"})  # may exist, are labeled weaker and never count toward the pass mark (A1)
LICENSES_OK = ("project license (pending)",)  # a task whose license is not named here (or verified by code) is not loaded
DATA_ROOT = Path(__file__).resolve().parents[3] / "data" / "admission"
TASK_TYPES = ("code", "text")


class SuiteRefused(ValueError):
    def __init__(self, code: str, text: str) -> None:
        super().__init__(f"{code}: {text}")
        self.code = code


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    prompt: str
    grader: dict
    checks: str
    reference: str
    timeout_s: float
    source: dict

    @property
    def grader_kind(self) -> str:
        return self.grader["kind"]

    @property
    def counts(self) -> bool:
        return self.grader_kind in CODE_GRADERS


@dataclass(frozen=True)
class Suite:
    suite_id: str
    task_type: str
    version: int
    tasks: tuple[Task, ...]
    preamble: str
    est_output_tokens: int
    hash: str

    def prompt_for(self, task: Task) -> str:
        return (self.preamble.strip() + "\n\n" if self.preamble.strip() else "") + task.prompt.strip()

    @property
    def counted(self) -> int:
        return sum(1 for t in self.tasks if t.counts)


def _canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def suite_hash(doc: dict) -> str:
    return "sha256:" + hashlib.sha256(_canonical(doc)).hexdigest()


def parse_suite(doc: dict) -> Suite:
    for key in ("suite_id", "task_type", "version", "tasks"):
        if key not in doc:
            raise SuiteRefused("suite_invalid", f"the suite has no `{key}`")
    if doc["task_type"] not in TASK_TYPES:
        raise SuiteRefused("suite_invalid", f"unknown task type {doc['task_type']!r}")
    tasks, seen = [], set()
    for raw in doc["tasks"]:
        tid = raw.get("id", "")
        if not tid or tid in seen:
            raise SuiteRefused("suite_invalid", f"task id {tid!r} is missing or repeated")
        seen.add(tid)
        grader = raw.get("grader") or {}
        kind = grader.get("kind")
        if kind not in CODE_GRADERS | WEAK_GRADERS:
            raise SuiteRefused("grader_unknown", f"{tid}: grader {kind!r} is not a known grader")
        lic = (raw.get("source") or {}).get("license")
        if lic not in LICENSES_OK:
            raise SuiteRefused("license_unverified", f"{tid}: the license {lic!r} has not been verified, so the task is not loaded")
        if kind == "sandbox_tests" and not (raw.get("checks") and isinstance(grader.get("n_checks"), int) and grader["n_checks"] > 0):
            raise SuiteRefused("suite_invalid", f"{tid}: a sandbox task needs checks and n_checks")
        if not raw.get("prompt") or "reference" not in raw:
            raise SuiteRefused("suite_invalid", f"{tid}: prompt and reference are required")
        tasks.append(Task(id=tid, title=raw.get("title", tid), prompt=raw["prompt"], grader=grader, checks=raw.get("checks", ""), reference=str(raw["reference"]),
                          timeout_s=float(raw.get("timeout_s", 60)), source=dict(raw["source"])))
    return Suite(suite_id=doc["suite_id"], task_type=doc["task_type"], version=int(doc["version"]), tasks=tuple(tasks), preamble=doc.get("preamble", ""),
                 est_output_tokens=int(doc.get("est_output_tokens", 200)), hash=suite_hash(doc))


def load_suite(task_type: str, root: Path | None = None) -> Suite:
    """The current suite for a task type. A type without one is `suite_missing`: it MUST NOT be routed (spec section 1)."""
    root = root or DATA_ROOT
    matches = sorted(p for p in root.glob("*/suite.yaml") if (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("task_type") == task_type)
    if not matches:
        raise SuiteRefused("suite_missing", f"there is no admission suite for task type {task_type!r}")
    return parse_suite(yaml.safe_load(matches[-1].read_text(encoding="utf-8")))
