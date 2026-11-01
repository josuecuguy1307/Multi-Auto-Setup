# The setup run API (what the Harness App calls)

The setup of the option an owner picked is a **run** on the coordinator, like an analysis or an options run. A screen (the Harness App, the panel, a chat adapter) starts it, reads it, and answers its pending decisions; it never writes a plan, a command or an approval by itself. Everything below exists in this branch (`phase3a-setup`); the HTTP routes are those of the owner's web screen (loopback, one-time link, session cookie), the Python names are the coordinator's front door that those routes call (`Coordinator` in `envrec_coordinator/server.py`, `AgentRuns` in `runs.py`).

## 1. Start

| Call | What it does |
|---|---|
| `GET /api/node/pick?id=<node_id>` · `Coordinator.pick_of(node_id)` | The option recorded for that machine, as a summary: `{node, picked_at, option, title, model, build {cell, backend, memory, asset_id, file, sha256, bytes}, memory_needed_gib, disk_needed_gib, labels, license, license_basis}`; `{"pick": null}` when nothing was picked. The pick is recorded by the OWNER's pick in an options run (`POST /api/run/pick`, `AgentRuns.pick_option` → `record_pick`). |
| `POST /api/install/start {"node": id}` · `Coordinator.start_install_run(node_id, via)` | Starts `protocol-setup` for the machine from its stored analysis and recorded pick. Returns the run view (section 2). **Typed refusals, no model call:** `status` is `failed` (not paired / not connected), `no_analysis`, `no_pick` or `no_model`, with a plain `reason`; the run has `finished` set and nothing started. |

`via` is `web` for the owner's own press (an OWNER event) and `claude-code` for the operator (counted apart, never as the owner's).

## 2. Read

`GET /api/run?id=<run_id>` · `Coordinator.run_view(run_id)` returns:

```
{id, kind: "install", node, node_id, status, reason, started, finished, via, summary, model,
 actors: {MODEL, GUARD, SCRIPT, OWNER, CLAUDE-CODE},          # who decided what so far
 plan: null | {hash, plan_id, machine, option, sensitivity, recipe, fit, total_download_bytes,
               steps: [{n, op, kind, title, command, writes[], bytes, undo, needs_owner_approval,
                        status: pending|done|undone|failed}],
               verification},
 pending: null | {question, text, options[{id,label,detail}], asked_at, data{...}},
 events: [{n, tag, type, text}],                              # the last 80 shown events
 result}
```

* **`status`** while running: `running`. Ends as one of the protocol's terminal states: `done` (verified), `rolled_back` (everything undone, nothing left), `cannot_install` (the plan could not be made; the typed reason is in the fact `setup.refused`), or `blocked_needs_human` / `stopped` (the run ended early, `reason` says why). `no_progress` is a `stopped` run.
* **The whole plan is data before the first approval.** `plan` is filled as soon as `build_plan` succeeds, and the plan is always built before any step can be asked, so the first `pending` approval already comes with the complete `plan` (every step's command, what it writes, its size in bytes, its undo, its status). `plan.hash` is the SHA-256 of the canonical plan; an approval is bound to it.
* **Facts** of the run (the run's own store; shown through `result` when it ends): `context.loaded`, `fit.rechecked {fits, needs_gib, budget_gib, basis, available_now_gib}`, `plan.built`, `plan.hash`, `plan.steps`, `step.N {op, status, reason?}`, `setup.action.K` (one per step really run or undone: the only thing that counts as progress), `setup.server {endpoint, bound_loopback, listening[], ready_s, resident_gib}`, `setup.minimal_call {ok, tested: "text", vision: "untested", endpoint, latency_ms, answer, tokens, tokens_per_s, resident_gib}` (tested), `setup.verified`, `setup.undone_all`, `setup.refused {reason, text}`.
* **Minimal call:** it sends one short **text** prompt; the record says `tested: "text"` and `vision: "untested"`. Vision stays untested unless an image is actually sent.

## 3. Decide (owner decisions)

A step is never run until the OWNER approves it. Each approval is a **pending decision** on the run:

```
pending = {question: "approve_run_<n>" | "approve_undo_<n>",
           text: "<human text of the same>",
           options: [{id: "approve"}, {id: "reject"}],
           data: {kind: "approve_step", plan_hash, step, of, action: "run"|"undo", title, command, writes[], bytes, undo}}
```

`POST /api/run/answer {"run": id, "question": <pending.question>, "option": "approve"|"reject", "by"?: "claude-code"}` · `Coordinator.answer_run(run_id, qid, option, by="owner")`.

* **Binding.** The answer `approve` is recorded as the OWNER event `setup_step_approved {run, node, plan_hash, step, action}` in the coordinator's log. The coordinator sends a write task **only** if it finds an event of that kind **tagged OWNER** for exactly that plan hash, step and action (`TaskDesk.start_setup_step` → conformance W1, W4); the node re-checks the same against the signed token (W2). An approval of one plan, step or action is not one of another.
* **An answer that is not the owner's.** `by: "claude-code"` writes the event as the operator's (tag CLAUDE-CODE); the task desk does not count it and the step ends `rejected` with `no_owner_approval`. **The gesture rule (Harness App).** An answer to `approve_run_<n>` / `approve_undo_<n>` counts as the owner's only when it comes with the human gesture that made it: `Coordinator.answer_run(run, question, option, by, channel, gesture)` takes `gesture` in `panel` (the owner's web screen, set by `/api/run/answer` itself, never by the request body), `panel_click`, `os_dialog`, `host_dialog` or `host_elicitation` (set by the Harness App core after the confirmation was answered: a ticket bound to the plan hash, the step and the action). An answer that only says `owner` is refused (`owner_authority_refused` in the log) and the decision stays open. A chat adapter holds only a bearer token for `/harness/v1`; it has no session on the owner's screen and cannot reach `/api/run/answer`.
* **Undo is its own request.** `undo_step N` (chosen by the harness loop's model, only for the latest step that is done) puts its own pending decision with `action: "undo"` and its own OWNER approval. It removes exactly what the plan wrote, stops what it started, and reports anything left; a non-empty `left` fails the undo.
* **Order.** Steps run in order; asking for step 3 before 1 and 2 is refused by the guard (a failed result, which counts as no progress).
* A decision nobody answers expires after 15 minutes (`owner did not answer in time`; nothing is changed).

## 4. Watch (live progress)

The run's `events` (and the run store's event stream) carry, besides the loop's own events:

| `type` | payload | meaning |
|---|---|---|
| `setup_plan` | `{plan_hash, steps, download_bytes}` | the plan was built |
| `setup_step` | `{step, action: run|undo, state}` | `waiting_for_owner`, `approved`, then the node task's states as they pass: `sent`, `accepted`, `running`, and the end `done` / `failed` / `rejected` / `timed_out` (with `reason`, a typed code such as `checksum_mismatch`, `no_owner_approval`, `server_not_loopback`) |
| `owner_answer` | `{question, option, plan_hash}` | the owner's (or operator's) answer, tagged OWNER or CLAUDE-CODE |

Poll `GET /api/run?id=` (the web screen's own method). A step that takes a long time (a download) shows `running` until it ends; byte-level progress is **not** reported in this version.

## 5. What the harness loop decides, and what it cannot

The model chooses one action per turn from `load_context`, `recheck_fit`, `build_plan` (state `prepare`) and `run_step N`, `undo_step N`, `finish_setup` (state `run`). It cannot write the plan, a command or a path, cannot approve, and cannot run steps out of order. Repeated identical reads, refused or invalid actions and failed results add nothing, so the loop's no-progress stop ends such a run.

## 5b. Undoing a finished setup, later, in a new run (Harness App)

The coordinator keeps, per machine, what a setup run left on it (`<state>/setups/<node>.json`: the whole plan that was run and where each step stands). The owner can ask a chat to undo it at any time; that is **a new run** of `protocol-undo`.

| Call | What it does |
|---|---|
| `POST /api/uninstall/start {"node": id}` · `Coordinator.start_uninstall_run(node_id, via)` · core goal `undo` · MCP tool `mas_undo` | Starts the undo run from the record. **Typed refusals, no model call:** `failed` (not paired / not connected) or `nothing_to_undo`. |
| `Coordinator.setup_of(node_id)` | What is recorded for a machine (hash, option, steps left), or None. |

* **Only what that plan wrote.** The undo plan is the recorded plan's steps that are done (a step that failed midway is offered like a done one: its undo removes the same paths), newest first, each with the undo the plan itself carries. A step that never ran is not offered.
* **The plan comes first, as words**, before any approval, with the recorded plan hash; it does not shrink as steps are undone.
* **Same machinery and same authority.** The loop's model chooses `load_setup`, `undo_step N` (the latest done step) and `finish_undo`; every `undo_step` puts `approve_undo_<n>` to the owner, answered only with a human gesture (section 3), bound to the recorded plan hash, step and action. The run ends `rolled_back` when nothing is left; the record is then removed. If the run ends earlier (`blocked_needs_human`), the record keeps what is still on the machine.
* The record is written when a setup run ends (also when it stopped half way) and rewritten when an undo run ends; a run that changed nothing leaves an earlier record as it was.

## 5c. Connecting a chat host (Harness App)

A chat the owner already uses (Open WebUI, Claude Desktop, Claude Code, Codex, OpenCode, Muse Code) is connected to Multi Auto Setup by a run of `protocol-connect-chats`, with the same plan, per-step approval, record and undo as a setup. The recipes are data (`skills/connect-recipes/references/recipes.json`, each host citing the tool's own documentation); the steps run on THIS computer, by the coordinator's own code, after an OWNER approval bound to the plan hash, step and action (`Coordinator.connect_step`, the same rule the task desk applies before it sends a step to a node).

| Call | What it does |
|---|---|
| `POST /api/connect/start {"host"?}` · `Coordinator.start_connect_run(host, via)` · core goal `connect_host` · `mas connect [host]` · MCP tool `mas_connect` | Starts the run. Without `host` the owner is asked "Which chats do you want to use Multi Auto Setup in?" with only the chats that were found (Open WebUI is offered when `uv` is there to install it) |
| core goal `disconnect_host` · `Coordinator.start_disconnect_run(host, via)` · `mas disconnect host` · `mas_disconnect` | A new run that undoes only what that connection wrote, from the record `setups/connect-<host>.json`; the same gesture rule |

**A manual Open WebUI install from an earlier session** (`~/mas-owui`) is removed by its own plan, host `openwebui-cleanup`: stop the server that was started from that folder (re-checked by pid, start time and command line), move the folder aside to `~/mas-owui.removed-<stamp>` (nothing is deleted: the owner deletes it when sure), verify. Its undo moves the folder back and starts the server again. The recipe `openwebui` refuses with `legacy_install_present` until that plan has run. After a connection the owner reads the Open WebUI administrator account on their own screen (`GET /api/openwebui/admin`, the panel's session only) or at their terminal (`mas openwebui-account`), never in a chat.

Typed refusals (a `nothing_to_connect` run, with the reason in the fact `setup.refused`): `unknown_host`, `not_detected`, `app_running` (Claude Desktop rewrites its file when it quits: quit it first), `config_is_jsonc` and `config_not_plain_json` (a settings file with comments is not rewritten), `ambiguous_keys`, `os_not_documented`, `uv_missing`, `port_in_use`, `already_connected`. A settings file is backed up first, other entries are never read out or touched, and the undo restores the copy when the file is as we left it, deletes a file we created, and otherwise removes only our entry. Open WebUI is installed pinned in its own folder, started on 127.0.0.1 only and offline, its first admin account is made through the source's own documented start-up variables (`WEBUI_ADMIN_EMAIL`, `WEBUI_ADMIN_PASSWORD`, "useful for headless/automated deployments"), and our function is registered through its API (`/api/v1/functions/create`, `/toggle`, `/valves/update`). The generated admin password is kept in `~/mas-openwebui/admin.secret` (mode 600) so that the undo can sign in and delete the function; **it should move to the operating system's keychain later** (the owner decided to keep the file for now). Single-user mode (`WEBUI_AUTH=False`) is not used: in the pinned source it signs anyone in as the administrator with no credential, and the default `CORS_ALLOW_ORIGIN` is `*`, so any web page could.

## 6. Known gaps (labeled, not hidden)

* When the pick no longer fits, `build_plan` refuses with `does_not_fit` and the numbers, but does **not** list variants that fit (the options step does).
* Sharded models and vision models (projector download and `--mmproj`) are built into the plan and covered by plan tests; neither has been downloaded and run end to end.
* "Measured here" does not yet replace the estimate in later options (the minimal call reports resident memory and speed; nothing stores them back).
* Windows and Linux: the per-OS code paths exist (process start, execute bit, commands shown); only macOS has run the node operations.
* Ollama (and any installed runtime) is not offered: options say `no_recipe_yet`.
