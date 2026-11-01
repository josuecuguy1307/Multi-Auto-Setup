"""The owner decision that has to come before an exam: running one uses the owner's machine time and plan quota, so it starts only after a pending decision, shown with its cost, is
answered by a human gesture. Same rule as the setup steps (docs/SETUP_API.md section 3, and the Harness App's gesture rule): an answer counts as the owner's only when it comes with
a gesture the owner made (the system dialog, the chat's own dialog, the panel). A bare "owner" answer is refused. A chat model's tool call never produces OWNER."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from envrec.skills import sbx

from envrec_coordinator.admission import strings as S
from envrec_coordinator.admission.exam import DEFAULT_THRESHOLD, ExamAborted, Runner, run_exam
from envrec_coordinator.admission.facts import AdmissionFacts, Combo, ExamOutcome
from envrec_coordinator.admission.suite import Suite
from envrec_coordinator.runs import OWNER_GESTURES  # one set of owner gestures for the whole coordinator (defined with the runs that check it)

Outbound = Callable[[dict], dict]  # the router's filter_outbound(intent, entry_chat), bound to the chat the intent goes to


class Refusal(Exception):
    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code, self.text = code, text


def admission_id(combo: Combo, suite: Suite, threshold: int) -> str:
    return "adm-" + hashlib.sha256(f"{combo.key}|{combo.model_sha256}|{combo.environment_version}|{suite.hash}|{threshold}".encode()).hexdigest()[:12]


def intent(kind: str, run_id: str, text: str, payload: dict, sensitivity: str = "public") -> dict:
    """A Harness App intent. The suites are the project's own public tasks, so the exam's intents are `public`; they still go through the outbound filter."""
    return {"kind": kind, "run_id": run_id, "actor": "GUARD", "sensitivity": sensitivity, "payload": payload, "text_fallback": text}


class AdmissionDesk:
    def __init__(self, facts: AdmissionFacts, *, outbound: Outbound | None = None, sandbox_ready: Callable[[], bool] | None = None, code_runner=None) -> None:
        self.facts, self.log = facts, facts.log
        self.outbound = outbound or (lambda i: i)
        self.sandbox_ready = sandbox_ready or sbx.docker_ready
        self.code_runner = code_runner
        self._open: dict[str, dict] = {}

    # ---- 1. the proposal: a pending decision with its cost -------------------------------------------------------------------------------------

    def propose(self, combo: Combo, suite: Suite, *, threshold: int = DEFAULT_THRESHOLD, machine_name: str | None = None, tokens_per_s: float | None = None,
                shared_quota_access: str | None = None) -> dict:
        aid = admission_id(combo, suite, threshold)
        if aid in self._open:
            return self._open[aid]
        of = sum(1 for t in suite.tasks if t.counts)
        if tokens_per_s and tokens_per_s > 0:
            secs = of * suite.est_output_tokens / tokens_per_s
            time_line = S.TIME_ESTIMATED.format(minutes=max(1, round(secs / 60)), tps=round(tokens_per_s, 1))
        else:
            secs, time_line = None, S.TIME_UNKNOWN
        quota = S.QUOTA_SHARED.format(access=shared_quota_access) if shared_quota_access else S.QUOTA_NONE
        data = {"kind": "approve_admission", "admission_id": aid, "fact": combo.key, "task_type": combo.task_type, "suite_id": suite.suite_id, "suite_hash": suite.hash, "tasks": of,
                "environment_id": combo.environment_id, "model_id": combo.model_id, "access_id": combo.access_id, "machine_id": combo.machine_id, "threshold": threshold,
                "early_stop": "first 3 fail, or the pass mark can no longer be reached", "estimated_seconds": secs, "estimate": "estimated" if secs else "not estimated", "quota": quota}
        text = S.APPROVE_TEXT.format(environment=combo.environment_id, model=combo.model_id, machine=machine_name or combo.machine_id, tasks=of, task_type=combo.task_type,
                                     suite=suite.suite_id, suite_hash=suite.hash[7:15], threshold=threshold, time_line=time_line, quota=quota)
        pending = {"question": S.APPROVE_QUESTION, "id": aid, "text": text, "options": [{"id": "approve", "label": S.OPT_APPROVE}, {"id": "reject", "label": S.OPT_REJECT}], "data": data}
        self._open[aid] = {**pending, "combo": combo, "suite": suite}
        self.log.add("admission_proposed", {"admission": aid, "fact": combo.key, "suite_hash": suite.hash, "threshold": threshold, "tasks": of, "estimated_seconds": secs, "tag": "GUARD"})
        self.outbound(intent("confirm", aid, text, {"question": S.APPROVE_QUESTION, "options": pending["options"], "data": data}))
        return pending

    def pending(self) -> list[dict]:
        return [{k: v for k, v in p.items() if k not in ("combo", "suite")} for p in self._open.values()]

    # ---- 2. the answer: only a human gesture counts ----------------------------------------------------------------------------------------------

    def answer(self, aid: str, option: str, *, by: str = "owner", gesture: str | None = None, channel: str | None = None) -> bool:
        p = self._open.get(aid)
        if p is None:
            return False
        if by != "owner":  # the operator or anything else: written as theirs (CLAUDE-CODE), never counted, the decision stays open
            self.log.add("admission_answer", {"admission": aid, "option": option, "by": "claude-code", "channel": channel}, owner=True)
            self.outbound(intent("text", aid, S.REFUSED_OPERATOR, {"state": "refused"}))
            return False
        if gesture not in OWNER_GESTURES:  # a bare "owner" answer (a chat adapter's tool path, a direct call) is refused
            self.log.add("owner_authority_refused", {"admission": aid, "option": option, "channel": channel, "gesture": gesture})
            self.outbound(intent("text", aid, S.REFUSED_NO_GESTURE, {"state": "refused"}))
            return False
        data = p["data"]
        if option == "approve":
            self.log.add("admission_approved", {"admission": aid, "fact": data["fact"], "suite_hash": data["suite_hash"], "threshold": data["threshold"], "gesture": gesture, "channel": channel}, owner=True)
        else:
            self.log.add("admission_rejected", {"admission": aid, "fact": data["fact"], "gesture": gesture, "channel": channel}, owner=True)
            del self._open[aid]
        return True

    def _approval(self, aid: str) -> str | None:
        """The id of an OWNER-tagged, unused approval of exactly this admission id (combination, suite hash, threshold), or None. The operator's and the code's never count."""
        used = {e.payload.get("approval") for e in self.log.events("admission_started")} - {e.payload.get("approval") for e in self.log.events("admission_released")}
        for e in reversed(self.log.events("admission_approved")):
            if e.effective_tag == "OWNER" and e.payload.get("admission") == aid and e.id not in used:
                return e.id
        return None

    # ---- 3. the start: looks the approval up BEFORE anything is asked of the environment -------------------------------------------------------------

    def start(self, combo: Combo, suite: Suite, runner: Runner, *, threshold: int = DEFAULT_THRESHOLD, stop_unreachable: bool = True, machine_name: str | None = None) -> ExamOutcome:
        aid = admission_id(combo, suite, threshold)
        approval = self._approval(aid)
        if approval is None:
            self.log.add("admission_refused", {"admission": aid, "fact": combo.key, "reason": "no_owner_decision", "tag": "GUARD"})
            raise Refusal("no_owner_decision", S.REASONS["no_owner_decision"])
        self.log.add("admission_started", {"admission": aid, "approval": approval, "fact": combo.key, "suite_hash": suite.hash}, owner=False)
        self._open.pop(aid, None)
        who = {"environment": combo.environment_id, "model": combo.model_id, "machine": machine_name or combo.machine_id}
        of = sum(1 for t in suite.tasks if t.counts)
        self.outbound(intent("progress", aid, S.PROGRESS_START.format(**who, tasks=of, threshold=threshold), {"state": "started", "tasks": of}))

        def on_task(n: int, total: int, title: str, res) -> None:
            verdict = S.PROGRESS_PASS if res.passed else S.PROGRESS_FAIL.format(why=S.TASK_FAIL.get(res.reason, res.reason))
            self.outbound(intent("progress", aid, S.PROGRESS_TASK.format(n=n, tasks=total, title=title, verdict=verdict), {"state": "running", "n": n, "tasks": total, "passed": res.passed}))

        try:
            out = run_exam(combo, suite, runner, self.facts, threshold=threshold, stop_unreachable=stop_unreachable, code_runner=self.code_runner, sandbox_ready=self.sandbox_ready, on_task=on_task)
        except ExamAborted as exc:
            if exc.ran == 0:  # nothing was asked of the environment: the owner's approval is not used up
                self.log.add("admission_released", {"admission": aid, "approval": approval, "reason": exc.reason, "tag": "GUARD"})
            self.outbound(intent("text", aid, S.RESULT_ABORTED.format(**who, reason=S.REASONS.get(exc.reason, exc.reason)), {"state": "aborted", "reason": exc.reason, "ran": exc.ran}))
            raise
        label = self.facts.view(combo, suite, threshold).label
        self.outbound(intent("show_data", aid, (S.RESULT_ADMITTED if out.admitted else S.RESULT_NOT_ADMITTED).format(**who, label=label, task_type=combo.task_type),
                             {"state": "done", "admitted": out.admitted, "label": label, "passed": out.passed, "of": out.of}))
        return out
