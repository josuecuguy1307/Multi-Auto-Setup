"""The coordinator's owner-facing words: one line per recorded step of a run (the owner panel and the chats show them), and the labels of a run's results. The product is in English; this is the one place to change that.
Words the model or a machine wrote (a reason, a name) are never in here: they are shown as they are."""

EVENT = {
    "action_chosen": "chose {action}{tool}{why}", "tool_run": "ran {tool} on the machine", "tool_ok": "ok", "tool_failed": "failed ({error})", "tool_facts": ", {facts} facts",
    "protocol_state": "protocol {from_} → {to} (by {by})", "run_requested": "asked for this run", "owner_answer": "answered {question}: {option}", "owner_answer_operator": " (the operator, for the owner)",
    "owner_pick": "picked waiting computer {waiting}", "owner_pick_option": "picked option {option}", "owner_confirmation": "{verdict} plan {plan_hash} ({mechanism})", "approved": "approved", "declined": "declined",
    "run_end": "run ended: {stop_reason}", "setup_plan": "the plan is ready: {steps} steps, {mib} MiB to download", "setup_step": "step {step} ({action}): {state}{reason}",
    "code_only_probe": "code ran {probe} ({status})",
}
LABEL_CODE_ONLY = "code only, no model: these are the probes' raw results; no agent interpreted them"
LABEL_AGENT_ANALYSIS = "agent analysis (a model chose the probes; code ran them)"
