"""Every owner-facing string of the admission exam, in one module. English, ASCII (check L1)."""

from __future__ import annotations

LABEL_MEASURED = "measured here: {passed}/{of}"
LABEL_NOT_ADMITTED = "measured here: {passed}/{of}, not admitted (threshold {threshold}/{of})"
LABEL_STOPPED = "measured here: {passed}/{of}, not admitted ({ran} of {of} tasks run; stopped early: {why}; threshold {threshold}/{of})"
LABEL_STALE = "stale: {passed}/{of} was measured with {changed}; measure again"
LABEL_PROVISIONAL = "provisional, not measured here"
LABEL_ABORTED = "not measured: the exam did not finish ({reason})"

STOP_FIRST_3 = "the first 3 tasks failed"
STOP_UNREACHABLE = "the threshold can no longer be reached"

CHANGED = {"suite_hash": "an earlier version of the suite", "model_sha256": "another model file", "model_id": "another model", "environment_version": "another version of the environment",
           "machine_id": "another machine", "access_id": "another access", "threshold": "another threshold"}

REASONS = {
    "sandbox_missing": "this machine has no sandbox, and model-written code never runs without one",
    "runner_unavailable": "the environment could not be reached",
    "no_owner_decision": "the owner has not approved this exam",
    "suite_missing": "there is no admission suite for this kind of task",
    "environment_error": "the environment failed while answering",
}
TASK_FAIL = {
    "wrong_answer": "the answer was wrong", "syntax_error": "the code did not parse", "exception": "the code raised an error", "timeout": "the answer took too long",
    "no_result": "the code stopped before the checks finished", "no_code": "the reply held no code", "no_json": "the reply held no JSON object", "not_equal": "the answer did not match",
    "no_answer_line": "the last line was not in the required form", "wrong_lines": "the lines did not match", "empty": "the reply was empty", "wrong_check_count": "the checks did not all run",
    "too_long": "the reply was longer than allowed", "missing_content": "the reply left out something it had to carry", "forbidden_content": "the reply held something it must not",
}

APPROVE_QUESTION = "approve_admission"
APPROVE_TEXT = ("Measure {environment} with {model} on {machine}? It runs {tasks} {task_type} tasks from suite {suite} ({suite_hash}). Pass mark {threshold}/{tasks}; it stops early when "
                "the first 3 fail or the pass mark can no longer be reached. {time_line} It uses this machine's time and the plan's quota ({quota}). Only you can approve this, with the "
                "system dialog, the chat's own dialog or the panel.")
TIME_ESTIMATED = "Estimated time: about {minutes} min (from {tps} tokens per second measured here)."
TIME_UNKNOWN = "Estimated time: not estimated (no speed has been measured on this combination)."
QUOTA_NONE = "no plan quota: runs on this machine"
QUOTA_SHARED = "tasks count against the quota shared by {access}"
OPT_APPROVE, OPT_REJECT = "Measure it", "Not now"

REFUSED_NO_GESTURE = "That answer did not come with your own gesture, so it was not counted. The decision is still open."
REFUSED_OPERATOR = "The operator cannot approve an exam. The decision is still open."
REFUSED_UNKNOWN = "There is no open decision with that id."

PROGRESS_START = "Measuring {environment} with {model} on {machine}: {tasks} tasks, pass mark {threshold}/{tasks}."
PROGRESS_TASK = "Task {n} of {tasks} ({title}): {verdict}."
PROGRESS_PASS, PROGRESS_FAIL = "passed", "failed ({why})"
RESULT_ADMITTED = "{environment} with {model} on {machine} is {label}. It can receive {task_type} tasks, including confidential ones."
RESULT_NOT_ADMITTED = "{environment} with {model} on {machine} is not admitted for {task_type} tasks: {label}."
RESULT_ABORTED = "The exam for {environment} with {model} on {machine} did not finish: {reason}. Nothing was stored, so it still counts as not measured."
