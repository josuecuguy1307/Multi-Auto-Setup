"""Every owner-facing string of the board package, in English, in one place (chat text, text fallbacks, approval prompts, errors). Nothing else in the package writes text the owner reads."""

from __future__ import annotations

REFUSALS = {  # typed reasons the board gives for not doing something, in plain words
    "unknown_task": "That task does not exist on the board.",
    "unknown_lease": "That lease does not exist on the board.",
    "already_claimed": "Another worker already holds that task; it cannot be taken twice.",
    "not_open": "That task is not free.",
    "attempts_exhausted": "That task has used all its attempts.",
    "lease_expired": "That lease's deadline has passed; the task was freed.",
    "lease_not_live": "That lease is no longer active.",
    "extension_too_long": "The requested extension is more than the maximum allowed.",
    "lease_too_long": "The requested deadline is more than the maximum allowed.",
    "no_router_decision": "That task has no router decision yet; nobody can take it without one.",
    "wrong_node_for_route": "The router chose a different machine for that task.",
    "verifier_not_independent": "Whoever verifies cannot be the same machine that did the work.",
    "sensitivity_lowered": "A derived item cannot lower the sensitivity of its inputs; only the owner can.",
    "untested_capability": "A capability without a test run here (evidence and date) cannot appear on a machine's card.",
    "bad_entry": "That board entry is not valid.",
    "secret_key": "A field name in the content could carry a secret, so it was rejected.",
    "no_owner_approval": "The owner's approval for this exact decision is missing.",
    "not_a_human_channel": "That answer did not come from a human gesture, so it does not count as the owner's.",
    "decision_unknown": "That decision does not exist.",
    "decision_closed": "That decision was already answered.",
    "stale_decision": "That route decision is older than the task's current one.",
    "decision_expired": "That decision expired without an answer; nothing was changed.",
}

STATE_TEXT = {"open": "free", "claimed": "in progress", "done": "done and verified by code", "failed": "failed its test", "unverified": "result untested (hypothesis)", "exhausted": "out of attempts"}
FREE_REASONS = {"deadline_passed": "the deadline passed", "attempts_exhausted": "attempts ran out", "released_by_worker": "the worker released it", "reassigned": "the owner reassigned it"}

BOARD_EMPTY = "Board: no tasks."
BOARD_HEAD = "Board: "


def task_line(task_id: str, task_type: str, sensitivity: str, state: str, who: str, left: str, why: str) -> str:
    return f"- {task_id} ({task_type}, {sensitivity}): {state}{who}{left}{why}"


def left_text(seconds: float) -> str:
    return f" · {seconds:.0f} s left"


def posted(task: str) -> str:
    return f"Task {task} posted to the board; the router still has to decide where it runs."


def routed(task: str, environment: str | None, machine: str, evidence: str) -> str:
    return f"The router assigned task {task} to {environment + ' on ' if environment else ''}machine {machine} ({evidence})."


def escalated(task: str, environment: str | None, machine: str, step: int, evidence: str) -> str:
    return f"Task {task} moved up to escalation step {step}: {environment + ' on ' if environment else ''}machine {machine} ({evidence})."


def route_refused(task: str, code: str, reason: str) -> str:
    return f"The router could not place task {task} [{code}]: {reason}"


def dispatched(task: str, machine: str) -> str:
    return f"Task {task} was sent to {machine}."


def dispatch_refused(task: str, code: str, reason: str) -> str:
    return f"Task {task} could not be sent [{code}]: {reason}"


def escalation_summary(task: str, from_machine: str | None, to_environment: str | None, to_machine: str, step: int, evidence: str) -> str:
    return (f"Task {task} failed its code test on {from_machine or 'its machine'}. The router proposes escalation step {step}: "
            f"{to_environment + ' on ' if to_environment else ''}machine {to_machine} ({evidence}). Move it there?")


def escalation_declined(task: str) -> str:
    return f"You declined the escalation of task {task}; it stays failed."


def decompose_summary(root: str, cap: int) -> str:
    return f"The director wants to split task {root} into up to {cap} subtasks. Each one still goes through the router, and every escalation still asks you. Allow it?"


def run_stopped(stop: str) -> str:
    return {"finished": "The director finished the work.", "blocked_needs_human": "The director stopped and needs you.", "no_progress": "The director made no progress and was stopped.",
            "refused": "The router refused the task, so the run stopped.", "turn_cap": "The run reached its turn limit and stopped."}.get(stop, f"The run stopped: {stop}.")


def taken(node: str, task: str, attempt: int, lease_s: float) -> str:
    return f"{node} took task {task}: attempt {attempt}, {lease_s:.0f} s to finish."


def running(task: str, step: str, note: str, extend_s: float | None) -> str:
    return f"Task {task} in progress, step \"{step}\": {note}" + (f" (deadline extended {extend_s:.0f} s)" if extend_s else "")


def done(task: str) -> str:
    return f"Task {task} finished and the code test passed it."


def failed(task: str) -> str:
    return f"Task {task} finished but did NOT pass its code test; the router decides the next step."


def unverified(task: str) -> str:
    return f"Task {task} returned a result with no code test: it is stored as a hypothesis, not as a fact."


def freed(task: str, exhausted: bool, why: str) -> str:
    return f"Task {task} {'is out of attempts' if exhausted else 'was freed'}: {why}."


def exhausted_needs_owner(task: str) -> str:
    return f"Task {task} used all its attempts and needs a decision from you."


def owner_decided(word: str, decision_id: str) -> str:
    return f"The owner {word} decision {decision_id}."


OWNER_WORDS = {"approve": "approved", "reject": "rejected"}


def owner_chose(option: str) -> str:
    return f"chose \"{option}\" for"


def operator_answer(decision_id: str) -> str:
    return f"The answer to {decision_id} came from the operator, not from you, so it does not count. Still waiting for your decision."


DIRECTOR_SAYS = {"accept_result": "accepted the result of", "reject_result": "rejected the result of", "finish": "finished the work", "escalate_to_owner": "has a question for you", "submit_plan": "sent you a plan to approve"}


def director(action: str, detail: str) -> str:
    return f"The director {DIRECTOR_SAYS[action]}{detail}."


def confirm_plain(summary: str, decision_id: str, minutes: float) -> str:
    return f"{summary}\nDecision {decision_id}. Answer with the approval dialog (approve or reject); if you do not answer within {minutes:.0f} minutes, nothing changes."


def confirm_write(summary: str, command: str, undo: str, decision_id: str, minutes: float) -> str:
    return (f"{summary}\nExact command: {command}\nHow to undo it: {undo}\nDecision {decision_id}. Only your gesture in the system dialog approves it; "
            f"if you do not answer within {minutes:.0f} minutes, nothing is changed.")


def ask_choice(summary: str, lines: str, decision_id: str) -> str:
    return f"{summary}\n{lines}\nDecision {decision_id}: choose with the dialog; if you do not answer, nothing changes."


WITHHELD_NOTICE = "There is activity on the network whose details do not leave your network. See it in Open WebUI with the local model or in the panel."
OPTION_APPROVE, OPTION_REJECT = "Approve", "Reject"


def plan_summary(root: str, items: list[dict]) -> str:
    lines = []
    for i in items:
        if i["kind"] == "verification_slot":
            lines.append(f"  + one verification of {i['for']} ({i['task_type']}, {i['sensitivity']}), on a different machine")
        else:
            lines.append(f"- {i['task_id']} ({i['task_type']}, {i['sensitivity']}): input {i['input_ref']}, graded by {i['test_ref']}")
    return (f"The director asks to run these subtasks for task {root}:\n" + "\n".join(lines) + "\nOnly these are allowed. A new subtask, or an extra verification, asks you again. Allow them?")


def grade_refused(task: str, code: str, reason: str) -> str:
    return f"Task {task} could not be graded by code [{code}]: {reason} Its result is kept as a hypothesis, not as a fact."


def plan_refused(reason: str) -> str:
    return f"You approved the plan, but the router did not accept it: {reason} Nothing was registered; the director can submit it again."


def plan_approved(n: int) -> str:
    return f"Plan approved: {n} item(s) registered. Each still goes through the router."


UNAPPROVED = "waiting for your approval (the director must submit a plan)"
PLAN_WITH_OWNER = "the plan is waiting for your answer"
DECLINED = "you declined it"


def owner_task(task: str) -> str:
    return f"Your task {task} is on the board; the director will split it into subtasks and ask you before anything runs."
