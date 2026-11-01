"""Every owner-facing word of the Harness App (chat text, text fallbacks, dialogs, approval prompts, cards, errors). The product is in English; `lang` stays a parameter so a language can be added here again, in this one module. Words that come from a model or from a machine never pass through here: they are escaped, quoted data."""

STR = {
    "en": {
        "network": "Your network", "coordinator": "Coordinator", "online": "online", "busy": "busy", "offline": "offline", "legend_local": "data stays on the machine",
        "legend_cloud": "goes to the cloud", "legend_lock": "confidential", "click": "Click a machine to open its card", "no_nodes": "No paired machines yet",
        "machine": "Machine", "system": "System", "hardware": "Hardware", "ai": "AI available", "findings": "Agent findings", "unknown": "unknown",
        "tested": "tested", "inferred": "inferred", "declared": "declared", "verified": "verified", "hypothesis": "hypothesis", "options": "Options for this machine",
        "local": "local model", "api": "own API key", "cli": "subscription via CLI", "provisional": "provisional, not measured here", "fits": "fits", "nofit": "does not fit",
        "plan": "Setup plan", "command": "Exact command", "writes": "Writes to", "undo": "How to undo", "size": "Size", "approve_panel": "Approved in the owner panel",
        "run": "Run", "actors": "Who decided", "last_seen": "last seen", "facts": "facts", "pick": "Pick", "status_pending": "pending", "status_approved": "approved",
        "status_running": "running", "status_done": "done", "status_failed": "failed", "status_undone": "undone", "verify": "Verification", "ok": "ok", "not_ok": "not verified",
        "withheld": "Withheld: confidential", "cloud_note": "This chat is in the cloud: it only receives summaries", "pick_text": "Pick an option for this machine.",
        "no_reco": "The options are not ordered by preference and nobody recommends one: the owner chooses.", "doing": "doing now", "idle": "idle", "actor": "acted",
        "install_note": "Installing only happens through Multi Auto Setup's setup flow with your approval; no other path installs anything for you.", "weak": "weaker confirmation: no Touch ID or Windows Hello",
        "strong": "verified with Touch ID or the account password", "expl": "the model's explanation, not verified", "memory": "Memory", "needs": "needs", "budget": "usable", "total": "total",
        "free": "free", "size": "Size", "tk_approved": "approved", "tk_declined": "declined", "tk_timeout": "no answer in time", "tk_unavailable": "unavailable", "tk_expired": "expired", "tk_rejected": "voided", "tk_waiting": "waiting", "agent_analysis": "agent analysis (a model chose the probes; code ran them)", "task": "task",
        "role_coord": "receives the paired machines and keeps their record", "role_node": "paired machine", "no_analysis": "no stored analysis: its resources and what it has installed are not known yet",
        # ---- preparing a machine (the setup run): the plan, each approval, the progress
        "pl_head": "Setup plan for {machine}: {option}", "pl_fit": "Memory: needs {needed} GiB; {budget} GiB may be used ({basis}); {fit}", "pl_fit_unknown": "no memory figure in the plan",
        "pl_total": "{size} will be downloaded in total.", "pl_total_moved": "{size} will be moved on this computer; nothing is downloaded.", "size_moved": "{size} (moved, not downloaded)", "pl_step": "Step {n} of {of}: {title}", "pl_cmd": "Exact command", "pl_writes": "Writes to", "pl_undo": "How it is undone", "pl_nothing": "nothing",
        "pl_needs": "asks for your approval", "pl_no_approval": "needs no approval", "pl_hash": "Plan hash", "pl_verify": "At the end a minimal text call is made to the local server and its latency is measured",
        "pl_rule": "You approve each step yourself, one by one, and only with a gesture of yours: the system dialog (Touch ID or password), the chat's own dialog or the panel. This chat and its model cannot approve or undo anything.",
        "pl_status_pending": "pending", "ap_title": "Multi Auto Setup: approve a step", "ap_run": "Run", "ap_undo": "Undo", "ap_step": "{verb} step {n} of {of} on {machine}: {title}",
        "ap_summary": "{verb} step {n} of {of} on {machine}: {title}", "ap_size": "Size", "ap_ask_tool": "To approve it call mas_request_approval with this intent_id: the system dialog opens; your model cannot answer for you.", "ap_ask_cli": "Type yes below to approve this step; anything else declines it.",
        "sp_line": "{machine} · {verb} step {n} of {of} «{title}»: {state}{extra}", "sp_run": "preparing", "sp_undo": "undoing", "sp_plan": "{machine} · the plan is ready: {steps} steps, {size} to download", "sp_plan_moved": "{machine} · the plan is ready: {steps} steps, {size} moved, nothing downloaded",
        "st_waiting_for_owner": "waiting for your approval", "st_approved": "approved by you", "st_sent": "sent to the machine", "st_accepted": "the machine accepted it", "st_running": "running", "st_done": "done",
        "st_failed": "failed", "st_rejected": "rejected", "st_timed_out": "no answer in time", "mc_line": "{machine} · minimal call: {ok}, latency {latency} ms{rate}, answer «{answer}» (text tested; vision untested)", "mc_rate": ", {tps} tokens/s", "ap_undoing": "What is undone",
        "mc_ok": "ok", "mc_bad": "failed", "no_pick_hint": "Pick an option for that machine first.", "prep_refused": "It could not start: {reason}",
        "undo_note": "Undoing is its own request with its own approval: each step to undo asks you separately.",
        "ud_head": "Plan to undo what was set up on {machine}: {option}", "ud_only": "Only what that plan wrote is undone, with the undo the plan itself carries; the newest step first.",
        "ud_step": "Undo step {n} of {of}: {title}", "ud_does": "What is done", "ud_removes": "Removes", "sp_undo_plan": "{machine} · the plan to undo is ready: {steps} steps",
        "ud_none": "That machine has nothing set up to undo.", "ud_rule": "Each undo is asked separately and only you approve it, with a gesture of yours: the system dialog (Touch ID or password), the chat's own dialog or the panel. This chat and its model cannot approve anything.",
        # ---- texts that were literals in the core and the adapters
        "reply_hint": "Reply with the number or the id.", "bad_option": "«{given}» is not an option of this question. Valid options (use the number or the id): {valid}",
        "yes": "yes", "no": "no", "quant_unknown": "quantization —",
        "mcp_rejected": "The harness refused it ({error}). {message}", "mcp_nothing_new": "(nothing new)", "mcp_status": "run_id: {run_id} state: {status}", "mcp_waiting": "; waiting for the owner: {ids}",
        "mcp_answered": "Answer recorded (channel {channel}). Call mas_status to continue. Installing only happens through Multi Auto Setup's setup flow with the owner's approval.",
        "ap_host_timeout": "The host did not show the confirmation in time: nothing was approved. The owner can approve on the panel.", "ap_dismissed": "The owner dismissed the confirmation: nothing was approved.",
        "ap_host_answer": "The owner's answer in the host: {state}.", "ap_os_answer": "The owner's answer in the system dialog: {state}.", "ap_weak_note": " Weaker confirmation: this computer offers neither Touch ID nor Windows Hello.",
        "ap_if_not": " If it was not approved, nothing was done; the owner can approve on the panel.", "ap_none": "This chat offers no confirmation: the owner approves on the panel.",
        "shell_no_init": "The host did not answer the view's startup (ui/initialize).", "shell_no_result": "Connected to the host, but no tool result has arrived yet.", "shell_refused": "The host refused the view's startup: ",
        "shell_connecting": "Multi Auto Setup: connecting to the host…", "shell_no_cards": "This response has no cards to show.",
        "home_legend": "~ stands for your home folder.", "cn_verify": "At the end {what}",
        "mas_home_legend": "MAS_HOME is Multi Auto Setup's own folder on that machine.", "approvals_removes": "Removes", "approvals_writes": "Writes to",
        # ---- the live activity of the machines, as lines in a chat
        "act_started": "{machine} · {kind} run started", "act_tool": "{machine} · {tool}: {outcome}{facts} (chosen by {chooser}, run by {runner})", "act_ok": "ok", "act_failed": "failed ({error})", "act_facts": ", {n} facts",
        "act_choice": "{machine} · {actor} chose {action}", "act_protocol": "{machine} · the protocol moved {from_} → {to}", "act_answer": "{machine} · you answered {question}: {option}", "act_pick": "{machine} · you picked option {option}",
        "act_confirm": "{machine} · you {verdict} the plan {plan_hash} ({mechanism})", "act_approved": "approved", "act_declined": "declined", "act_end": "{machine} · the {kind} run ended: {status}",
        "act_refused": "{machine} · {what}: {reason}", "act_probe": "{machine} · code ran {probe} ({status})", "act_more": "{machine} · {n} more steps: {items}", "act_task": "{machine} · task {type}: {state}",
        "act_nothing": "Nothing new on the machines.", "act_in": " in {s} s", "activity_title": "Live activity", "activity_empty": "Nothing has happened on the machines yet.",
        "cn_head": "Plan to connect {option} to Multi Auto Setup on {machine}", "cn_none": "No chat that can be connected was found on this computer.",
        "analysis_from": "stored analysis", "ago": "", "secs": "s ago", "never_seen": "no signal yet", "now": "Now", "paths_withheld": "(file paths are not included in this text)",
    },
}


def t(lang: str, key: str) -> str:
    return STR.get(lang, STR["en"]).get(key) or STR["en"].get(key, key)

# Labels of the machine facts the cards show, by fact key. A key without a row is shown by its own name (we never invent a friendlier word for a fact we do not know).
LABELS = {
    "en": {"machine.os.name": "Operating system", "machine.os.version": "Version", "machine.os.build": "Build", "machine.os.arch": "Architecture", "machine.os_family": "Family", "machine.os.family": "OS family",
           "machine.ram_total_gib": "Total RAM (GiB)", "machine.ram_available_gib": "Available RAM (GiB)", "machine.vram_gib": "VRAM (GiB)", "machine.disk_free_gib": "Free disk (GiB)",
           "machine.gpus": "Graphics cards", "machine.gpu_count": "GPU count", "machine.model_path": "Where models run", "machine.largest_model_params_b": "Largest model with the free memory (B)",
           "machine.largest_model_limited_by": "Limited by", "machine.runtimes": "Model runtimes", "machine.runtime_count": "Runtime count", "machine.ai_clis": "AI command-line tools",
           "machine.ai_cli_count": "CLI count", "machine.api_key_variable_names": "API key variables (names only)", "machine.programs_count": "Installed programs", "machine.usable_memory_gib": "Usable memory for a model (GiB)", "machine.memory_margin_pct": "Margin left out (%)",
           "machine.largest_model_by_total_ram_params_b": "Largest model by total RAM (B)"},
}


def label_for(lang: str, key: str) -> str:
    return LABELS.get(lang, LABELS["en"]).get(key) or key


LIMIT_NAMES = {"en": {"available_ram": "available RAM", "ram_no_gpu_detected": "RAM (no dedicated GPU)", "vram": "VRAM", "disk": "free disk", "catalog_top": "the top of our catalog", "ram": "RAM"}}


def humanize(lang: str, key: str, value):
    """A fact value in the owner's words where the raw value is an internal name."""
    if key == "machine.largest_model_limited_by":
        return LIMIT_NAMES.get(lang, LIMIT_NAMES["en"]).get(str(value), value)
    return value
