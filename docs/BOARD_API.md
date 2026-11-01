# The typed board (T4b): API, intents and seams

Package `envrec_board` (coordinator side; nodes reach it only through the signed channel). Status on `phase4b-board`: board core, owner decisions, intent emission, the director's menu, the director/workers loop and a SIMULATED-LOCAL end-to-end run are built and tested against the approved `docs/ROUTER_API.md` (with a stand-in router until T4a merges).

## 1. The six message types (NETWORK_CONTRACT section 5)

| Type | Entry | Payload (besides the envelope `task_id`, `task_type`, `sensitivity`, `actor`, `tag`) |
|---|---|---|
| node card 5.1 | `board.node_card` | node_id, os_family, hardware, inference, harnesses, capabilities `[{name, evidence_id, date}]` (a capability without evidence and date is refused) |
| task 5.2 | `board.task` | input, expected, test, created_by, optional `parent` (subtask of) and `verifies` (verification of) |
| lease 5.3 | `board.lease` | lease_id, node_id (the router's `machine_id`), deadline, attempt (the capability token is minted elsewhere and never stored here) |
| progress 5.4 | `board.progress` | lease_id, step, note, optional `extend_s` (capped) |
| result 5.5 | `board.result` | lease_id, output, evidence, test_result `{passed, by:"code"}`, evidence_status `tested` or `hypothesis` |
| published skill 5.6 | `board.published_skill` | skill, hash (only with tests passed) |

Other entries: `board.route_decision` (a `RouteDecision`, recorded, never made here), `board.route_refusal`, `board.escalation_proposed` / `board.escalation_declined`, `board.origin`, `board.dispatched`, `board.run_stopped`, `board.lease_expired` / `board.lease_released` (typed reason), `board.refused` (GUARD, typed `refusal`), `board.decision_pending` / `board.decision_answered` / `board.decision_used`, `board.director_action` (MODEL), `board.director_refused` (GUARD).

## 2. Rules the board enforces

- One live lease per task (no double claims, also under threads). A lease has a deadline; `sweep()` frees an expired lease with reason `deadline_passed`, or `attempts_exhausted` when the cap is reached. Work on an expired lease is refused. Deadline, attempt cap, lease maximum and extension maximum are coordinator settings.
- A task cannot be taken without a recorded router decision that names the claimer (`no_router_decision`, `wrong_node_for_route`). A verification task cannot be taken by the machine that did the work (`verifier_not_independent`).
- A derived task never lowers sensitivity (`sensitivity_lowered`). A result without a code grade is stored as a hypothesis (`unverified`). A payload key that could carry a secret is refused.
- Append-only: the log has only `append` and `entries`; `Board.replay(entries)` rebuilds the same state without reading a clock. `JsonlLog` persists under the state dir (`~/mas-t4b-state/board.jsonl` in SIMULATED-LOCAL).

## 3. Owner decisions (`DecisionDesk`)

Reassign, escalate beyond what the owner pre-approved, and any machine write are pending decisions bound to a hash of exactly what they approve, used once, expiring after 15 minutes. `answer(decision_id, option, channel=, by=)` needs `channel` in `os_dialog`, `host_dialog`, `panel` and an explicit `by` (no default, so a bare answer never counts); a hook can run inside an OWNER approval, and that is where the router's owner event is written; `by="claude-code"` is recorded as CLAUDE-CODE and never counts; anything else is refused. `ModelTools` (the only surface a chat model reaches) has no way to answer, reassign or claim.

## 4. Intents emitted (T2b schema)

| Board change | Intent |
|---|---|
| task posted / routed / taken / running / done / failed / unverified / freed | `progress` (`payload.state`), then `show_data` (`data_kind: "board"`) for posted, taken, result and freed |
| attempts exhausted, refusals, operator's answer | `text` |
| decision pending | `confirm` (approve/reject; a machine write carries command, undo and plan_hash) or `ask_choice` (numbered options) |
| owner answered | `progress` (`owner_decided`) |
| director accepts / rejects / finishes / asks | `progress` (`director_*`) |

Every intent carries an English `text_fallback` that stands on its own; all owner-facing text lives in `envrec_board/strings.py`. Cloud sinks are fail-closed: confidential never reaches them (a content-free notice does), and until T4a's outbound filter is installed only `public` does.

## 5. What T2b must wire (nothing in T2b's files was touched)

1. Accept `data_kind: "board"` in `intents.DATA_KINDS` (one word) and render its compact payload; the text fallback already stands alone.
2. Pass `IntentEmitter(..., validate=<T2b validate>)` and a sink per adapter, `kind="local"` for Open WebUI with a local model and the panel, `kind="cloud"` for Claude Desktop and Meta AI.
3. Call `DecisionDesk.answer(...)` only from a real human gesture, with `channel` set to `os_dialog` (Claude Desktop OS dialog), `host_dialog` (Open WebUI native confirm) or `panel`, and `by="owner"`. Never from a chat model's tool call.
4. Route `show_data` / `progress` / `confirm` from the emitter through the same approval and run endpoints as setup (`POST /api/run/answer` shape).

## 6. Against the router API (`docs/ROUTER_API.md`, copied here as a reference; T4a's copy is the source)

Built against the 2026-10-30 23:07 version: `router_port.py` (the nine calls as a Protocol), `plan.py` (the owner's plan), `team.py` (`TeamRun`), `director.py` (menu, view). T4a has not merged an implementation, so tests and the end-to-end run use `tests/board_sim.py`: a stand-in that follows the API's types, refusal codes, calls and rule order (exclusion, result-of levels, decompositions with slots, grader kinds) and a real HTTP model server on 127.0.0.1. Swapping in the real router means passing it as `router=` (and `outbound_filter=router.filter_outbound`).

How the loop uses the API:
- **The plan.** The director posts subtasks (unrouted) and then picks `submit_plan`. The owner gets ONE decision that lists every task (id, type, sensitivity, input reference, grader) and, for each listed subtask, one verification slot on a different machine. Answered by a human gesture (`os_dialog`, `host_dialog` or `panel`, with `by` explicit), it runs, from inside that answer, `record_owner_decision(parent, [{id, type, sensitivity}...], gesture)` and then `approve_decomposition(parent, subtasks, owner_event_id)`, which the router re-checks against its own stored event (a refusal is shown to the owner and nothing is registered). Then each listed task is registered with `own_subtask(task_id, decomposition_id, subtask_id)`, matched by the listed id; a verification takes its subtask's slot, whose id is `verify-of:<subtask>`. A new subtask, or a verification beyond its slot, is unapproved until another plan is approved. An escalation inside a task needs no new plan.
- **Routing.** `route(Task)` with `created_by = PLANNER_ACCEPTED`, `entry_chat` from the adapter, `payload_ref`, optional `input_refs` and, for a verification, `payload_ref: instr:verify`, `input_refs: [result-of:<task>]` and `exclude = {machine_ids: [the machine that did the work], environment_ids: [its environment]}`. A verification is routed under the type of the task it verifies and is never lower in sensitivity than it. The decision or refusal is recorded (ROUTER / GUARD); `effective_sensitivity` can raise the task's level on the board. A fatal refusal (`confidential_from_cloud_chat`, `sensitivity_missing`) stops the run; any other (`provisional_only`, `no_candidate`, `no_local_candidate`, ...) parks the subtask and the director sees the code and reason.
- **Running.** The named machine takes the subtask with a lease, `dispatch` sends it, `result_of(handle)` returns an `output_ref` (the board stores references, not content), `grade(task_id, output_ref)` returns the code verdict and `report_result` re-enters it. The grader is set with `set_grader` when the task is registered, from the task's `test_ref` (`equals|contains|regex|sha256|run_tests:<value>`), which the owner sees in the plan; a verification uses a regex on the verifier's answer. No grader, `weak_grader`, `sandbox_missing` or `output_missing` leaves the result a hypothesis and reports nothing.
- **Second guard.** `verifier_not_independent` stays at claim time, in case a router ignores `exclude`.
- **Escalation.** A returned escalation is a pending owner decision unless `TeamPolicy.preapproved_escalations` covers its step (default 0: every escalation asks). `escalation_exhausted` is recorded and shown.
- **Cloud chats.** Every intent for a cloud chat goes through `filter_outbound(intent, entry_chat)`; a confidential intent that comes back without `redacted: true` is withheld.

Escalations within a task run up to the router's maximum (`TeamPolicy.preapproved_escalations`, default 2) without a question; each is announced to the owner as a progress notice (`escalated`). Set it to 0 to make every escalation ask; the draft's freedom dial will tune it.

All earlier API assumptions are now defined by `docs/ROUTER_API.md` sections 10-12 (grader spec `{kind, value}`, `instr:` references, owner event and id matching).

Still not wired: T3b's draft as `TeamPolicy.from_draft(draft)` (conservative default today), the director as a real model (a scripted director stands in), and the real router.

## 7. Coverage matrix

The board is pure Python with no OS-specific code, so one implementation covers all targets; only macOS arm64 has run it.

| OS | Status |
|---|---|
| macOS arm64 | SIMULATED-LOCAL (unit + conformance on this Mac) |
| macOS x86_64, Windows 10/11, Debian/Ubuntu, Fedora (x86_64/arm64) | UNTESTED (no OS-specific code; file log uses `pathlib` and UTF-8 text) |
