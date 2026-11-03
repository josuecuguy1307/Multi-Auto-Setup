"""The exam itself: ask the environment each task in the suite's fixed order, grade by code, apply the threshold and the early stop, store the result. It does not choose a route, a model
action or an environment, and it does not start by itself: `decision.AdmissionDesk.start` is the only caller, after the owner's approval.

Tags: the model's replies are MODEL (`admission_reply`); validation, grading, stops and the stored fact are GUARD; the owner's start is OWNER (see decision.py). SCRIPT is not used."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from envrec.skills import sbx

from envrec_coordinator.admission import strings as S
from envrec_coordinator.admission.facts import AdmissionFacts, Combo, ExamOutcome, TaskResult
from envrec_coordinator.admission.graders import CodeRunner, SandboxMissing, grade
from envrec_coordinator.admission.suite import Suite

DEFAULT_THRESHOLD = 8
FIRST_N = 3


@dataclass(frozen=True)
class Reply:
    text: str
    seconds: float
    timed_out: bool = False  # the environment did not answer in time: the model's failure, counted as one
    proof: dict | None = None  # what the NODE recorded about this call, verified by the coordinator before it got here (envrec_net.envrun.verify_proof): host, times, the API's response id and model field, bytes


class RunnerAborted(Exception):
    """The environment could not be asked at all (offline, rejected, not installed): the exam's problem, never the model's. Nothing is counted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Runner(Protocol):
    """Asks one prompt of the exact combination being measured. The real one is the router's `dispatch` (docs/ROUTER_API.md section 2); tests use stand-ins."""

    def ask(self, prompt: str, *, task_id: str, timeout_s: float) -> Reply: ...


class ExamAborted(Exception):
    def __init__(self, reason: str, ran: int) -> None:
        super().__init__(reason)
        self.reason, self.ran = reason, ran


def early_stop(passed: int, ran: int, of: int, threshold: int, stop_unreachable: bool) -> str | None:
    """The first 3 tasks failed -> stop. Optionally (on by default) stop as soon as the pass mark can no longer be reached."""
    if ran == FIRST_N and passed == 0:
        return "first_3"
    if stop_unreachable and ran < of and passed + (of - ran) < threshold:
        return "unreachable"
    return None


def run_exam(combo: Combo, suite: Suite, runner: Runner, facts: AdmissionFacts, *, threshold: int = DEFAULT_THRESHOLD, stop_unreachable: bool = True,
             code_runner: CodeRunner | None = None, sandbox_ready: Callable[[], bool] | None = None, on_task: Callable[[int, int, str, TaskResult], None] | None = None) -> ExamOutcome:
    """Run the exam and store the fact. Raises ExamAborted (and stores nothing but an abort event) when it could not be finished for a reason that is not the model's."""
    counted = [t for t in suite.tasks if t.counts]
    of = len(counted)
    if of < threshold:
        raise ExamAborted("threshold_above_suite", 0)
    needs_sandbox = any(t.grader_kind == "sandbox_tests" for t in counted)
    if needs_sandbox and not (sandbox_ready or sbx.docker_ready)():  # before any model call: no quota is spent on an exam that cannot be graded
        facts.aborted(combo, "sandbox_missing", 0)
        raise ExamAborted("sandbox_missing", 0)
    results: list[TaskResult] = []
    stop: str | None = None
    for n, task in enumerate(counted, 1):
        try:
            reply = runner.ask(suite.prompt_for(task), task_id=task.id, timeout_s=task.timeout_s)
        except RunnerAborted as exc:
            facts.aborted(combo, exc.reason, len(results))
            raise ExamAborted(exc.reason, len(results)) from None
        ref = facts.log.store.put_raw(reply.text).sha256
        facts.log.add("admission_reply", {"fact": combo.key, "n": n, "task": task.id, "reply_ref": ref, "seconds": round(reply.seconds, 2), "timed_out": reply.timed_out, **({"proof": reply.proof} if reply.proof else {})})
        if reply.timed_out:
            res = TaskResult(task.id, False, task.grader_kind, "timeout", reply.seconds, ref)
        else:
            try:
                g = grade(task, reply.text, code_runner=code_runner)
            except SandboxMissing:
                facts.aborted(combo, "sandbox_missing", len(results))
                raise ExamAborted("sandbox_missing", len(results)) from None
            res = TaskResult(task.id, g.passed, g.grader, g.reason, reply.seconds, ref)
        results.append(res)
        facts.log.add("admission_task", {"fact": combo.key, "n": n, "task": task.id, "passed": res.passed, "grader": res.grader, "reason": res.reason, "tag": "GUARD"})
        if on_task:
            on_task(n, of, task.title, res)
        stop = early_stop(sum(r.passed for r in results), n, of, threshold, stop_unreachable)
        if stop:
            facts.log.add("admission_early_stop", {"fact": combo.key, "after": n, "why": stop, "tag": "GUARD"})
            break
    out = ExamOutcome(combo, suite.suite_id, suite.hash, threshold, of, tuple(results), stop, stop_unreachable)
    facts.write(out)
    return out


def now() -> float:
    return time.time()
