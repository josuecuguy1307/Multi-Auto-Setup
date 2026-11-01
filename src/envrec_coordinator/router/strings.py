"""Every owner-facing string of the router, in English, in one place (language rule, 2026-10-30). Codes are the stable part; the text may be reworded without touching the rule."""

from __future__ import annotations

REFUSALS: dict[str, str] = {
    "confidential_from_cloud_chat": "This task is confidential, so it can only be created from the owner panel or a local chat, not from a cloud chat.",
    "sensitivity_missing": "This task has no sensitivity level. Choose public, internal or confidential before it can be routed.",
    "unknown_task_type": "There is no environment set up for this kind of task yet, so it was not routed.",
    "no_candidate": "No environment on your computers can take this task right now. The details say what each one is missing.",
    "no_local_candidate": "This task is confidential and none of your computers has a local environment ready for it. Nothing was sent anywhere else.",
    "provisional_only": "This task is confidential and the only local combinations have not been measured on your computer yet. Confidential tasks only go to measured combinations.",
    "sandbox_missing": "This environment can write and run code, and this computer has no sandbox ready for it, so it was not used.",
    "outside_workspace": "The environment tried to write outside its task workspace. What it wrote was removed and the task failed.",
    "automated_use_unreviewed": "The vendor's terms for automated use of this tool have not been cleared by you, so it is shown as an option but never used automatically.",
    "escalation_exhausted": "The task failed on every environment it was allowed to try (at most two escalations), so it stopped. Nothing else will be tried without you.",
    "stale_decision": "That result is for a decision that is no longer the task's latest one, so it was ignored.",
    "weak_grader": "Only a result checked by code can pass or fail a task; this one was not, so it was ignored.",
    "payload_missing": "The task's input could not be found, so it was not sent.",
    "dispatch_rejected": "The computer refused to run the task. The details say why.",
    "no_owner_origin": "Nobody has created or approved this task yet, so it was not sent. Create it from the owner panel or accept the suggestion first.",
    "stale_recipe": "The environment's recipe on this computer is not the one the router used, so nothing was run.",
    "unsupported_os": "That environment does not run on this computer's operating system.",
    "not_installed": "That environment is not installed on the computer chosen for it.",
    "busy": "That computer is already running as many tasks as it is allowed to.",
    "node_offline": "That computer is not connected, so the task was not sent.",
    "no_grader": "No code grader was registered for this task, so its output cannot be checked.",
    "output_missing": "That output could not be found, so it was not graded.",
    "input_missing": "An input of this task could not be found, so it was not sent.",
    "unknown_input": "An input of this task comes from a task this router does not know, so it is treated as confidential.",
    "confidential_cloud_environment": "This input is confidential, so it cannot be sent to a cloud environment. Nothing was sent.",
    "no_sender": "No way to send the task to a computer is connected, so it was not sent.",
    "machine_offline": "The computer for this task is offline (machine offline), so nothing was sent. Nothing is retried on its own: create the task again when the computer is back.",
}

REASONS: dict[str, str] = {  # why an environment was left out on a machine (typed, shown in details)
    "not_installed": "not installed",
    "no_access": "no access (no key, no plan, no local model server)",
    "machine_offline": "machine offline",
    "server_not_running": "its local model server is not running",
    "not_signed_in": "not signed in",
    "unsupported_os": "does not run on this operating system",
    "closed_by_owner": "closed by the owner",
    "not_paired": "not one of the owner's paired computers",
    "sandbox_missing": "needs a sandbox this computer does not have",
    "automated_use_unreviewed": "automated use not cleared by the owner",
    "cloud": "cloud environment (not allowed for confidential tasks)",
    "provisional": "not measured on this computer (not allowed for confidential tasks)",
    "provider_not_approved": "provider not approved by the owner for internal tasks",
    "excluded_machine": "excluded by the task (this machine)",
    "excluded_environment": "excluded by the task (this environment)",
    "not_admitted": "did not pass the admission exam for this task type",
}

OWNER_NOTICE = "Task {task_id} could not be completed: it failed on {tried} environment(s) and no further one is allowed. It needs you."
REDACTED_LINE = "Task {task_id} is {state} on {machine}. Its content stays on your computers."
EVIDENCE_MEASURED = "measured here {passed}/{of}"
EVIDENCE_NONE = "no data"


ACCESS_MISSING = "access missing: {names}"


def reason_text(code: str, detail: dict | None = None) -> str:
    """The owner-facing text of a left-out reason. An access that lacks variables names them (the NAMES, never a value)."""
    names = (detail or {}).get("missing") or []
    if code == "no_access" and names:
        return ACCESS_MISSING.format(names=(" or " if detail.get("any") else ", ").join(names))
    return REASONS.get(code, code)


def refusal_text(code: str) -> str:
    return REFUSALS.get(code, REFUSALS["no_candidate"])
