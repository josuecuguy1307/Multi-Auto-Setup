---
name: harness-tools
description: Use when the owner asks from a chat to run, route, measure or explain work across his computers, or to add or remove a key on a machine. Says which tool does what, in what order, and what you must never do.
---

# The harness tools (work across the owner's computers, from a chat)

The unit of work is a **task**: some text, a task type, a sensitivity. The harness, not you, chooses where it runs: a written rule over environments that passed an admission exam. You ask, explain and relay; the owner approves in his own system dialog.

## Which tool, when

| The owner says... | Call | Then |
|---|---|---|
| "Summarize / extract / clean / compute / fix this" | `mas_create_task` | Show him the route and its reasoning, then `mas_request_approval` with the `intent_id`. Follow with `mas_job` when something may have changed. |
| "Why would it go there?" / "What if the key is gone?" | `mas_explain_route` | Read the candidates, what was left out and why. It changes nothing. |
| "Is the local model good enough for this?" | `mas_run_admission` | `mas_request_approval`, then `mas_job`. A measurement is what lets a route be "measured here". |
| "Which model is best for this?" | `mas_measurements` | Read the ranking. It lists only what was MEASURED here, says when a measurement is stale, lists unmeasured candidates without ranking them. Never add a number that is not in it (no vendor or public figure). To measure another model, serve it, then `mas_run_admission`. |
| "What happened to it?" | `mas_job` | If a step waits for him, say so. Do not call it again in a row. |
| "What can my machines do?" / after a key or a setup | `mas_propose` | One proposal from the catalog and one question; present it as it is, add no options of your own. |
| "What do you know about me?" / "change that" / "do not use X" | `mas_profile` | `show`; `set`, `role`, `close`, `open` with HIS words. Closing is a question to him; `open` undoes it. |
| "Cancel that" | `mas_cancel` | The run id. What it already did stays. |
| "Add my Meta key on the Lenovo" | `mas_add_access` (see "Secret entry" below) | Nothing: the approval dialog and then the key window open on his screen by themselves. Follow with `mas_job`. |
| "Remove it" / "undo that" | `mas_remove_access` (`undo: true` to bring it back) | The approval dialog opens by itself. |

## Rules you keep

- **Sensitivity is derived, never given by you.** The harness reads the owner's profile (what counts as confidential for him) and the categories you say the text touches (`touches`: his categories, `none` or `unsure`); when it cannot tell it asks HIM in the chat and creates the task from his answer. You have no argument for a level. A confidential task cannot be created from a cloud chat: say "create it from a local chat or the owner panel" and stop. You never try another channel for him.
- **Say what you did not decide.** After a task is created, show the route and the reasoning the harness printed. If it says nothing is available, the cause is in the reasoning (`access missing: NAME`, `not installed`, `closed by the owner`): relay it, offer the fix the harness names, never a workaround.
- **You never choose the route.** The written rule does (cost class local, then API, then subscription; admission rate; measured time; fewer tasks). If he wants another route, explain why this one was chosen and what would change it (a measurement, an access, an owner policy): do not work around it.
- **Say what the evidence is.** "Measured here 9/10" is a measurement; "provisional" means nothing has been measured on that combination yet and the rule picked the cheapest one. Confidential tasks only go to measured combinations.
- **A check is code.** If he gave a check, it grades the result. Without one the result is shown, not graded, and nothing escalates. Never grade a result yourself or tell him it "looks right" as if it were a check.
- **Escalation is at most two steps** and never leaves what the sensitivity allows. If it is exhausted, tell him plainly that it needs him.
- **A refusal is an answer.** Read its code and reason aloud ("machine offline", "access missing: NAME", "provisional only"). Do not retry in a loop: nothing is retried silently, and neither do you.
- **You never see or ask for a key.** `access missing: NAME` is the whole story: offer `mas_add_access`.
- A result of a confidential task is not shown to a cloud chat. That is the harness working; tell him where to read it.

## Boards (a goal with several steps)

| The owner says... | Call | Then |
|---|---|---|
| "Build / prepare / close the month: several steps" | `mas_board_start` (goal, sensitivity) | `mas_board_show` until a plan is waiting for him |
| (a plan is waiting) | `mas_board_approve` (board_id, decision_id) | `mas_request_approval` with the `intent_id`; he approves the whole plan ONCE in his own dialog; then `mas_board_show` again |
| "What is it doing?" | `mas_board_show` | Read each subtask, where it ran, its code check, and anything waiting for him |

- The director (the coordinator's own model) breaks the goal into subtasks; you do not. Each subtask is routed by the written rule like any task; a confidential board stays on measured local routes at every step.
- Verification is done on another machine or environment than the one that produced the result; if none exists, say so (it is a refusal, not a failure of the work).
- After repeated failures the board asks the owner before the next escalation: that is the escalation warning. Read it to him as it is.
- A write that is not in the approved plan is blocked by the guard; do not try to get around it.

## The draft

`mas_draft` with `start` asks the agent to propose how the whole network is set up (which kinds of environments are open on each machine, what each machine is for). Follow the run with `mas_status`, answer its questions with `mas_answer`. When the owner wants a change, call `mas_draft` with `correct` and his words exactly as he said them; do not rewrite them. `approve` only asks: he approves in his own system dialog. A draft is a proposal until then and changes no route. If the coordinator has no model, say so; nothing is drafted.
