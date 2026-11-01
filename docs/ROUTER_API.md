# Router API (T4a, v0 draft for STOP 1)

Status: **approved at STOP 1 (2026-10-30); changes need the owner**. Language: every owner-facing string is English and lives in `router/strings.py`. T4b (board) and T4c (admission) build against this file. Nothing here is implemented yet. Normative sources it implements: `docs/NETWORK_CONTRACT.md` sections 6 and 10, `docs/RECOMMENDATION_SPEC.md` sections 5-8 and 10.4, `docs/HARNESS_SPEC.md` (ROUTER actor, R1-R3, "Harness, not encoder").

The router is the one place where code, not a model, chooses a combination. It is a GUARD policy: it never chooses a model's actions, never declares success, never widens what a sensitivity allows. The model never sees a menu of routes.

Where it lives: `src/envrec_coordinator/router/` (rule, sensitivity, outbound filter, dispatch), data in `skills/router-environments/references/` (catalog, accesses, owner policy, rule config). No OS or tool string in Python (P1, H1): CLI flags, paths and install commands are data.

## 1. Types

```
Sensitivity = "public" | "internal" | "confidential"      # highest of the inputs; only the owner lowers it
TaskType    = <a slug>                                    # data: the `task_types` of the catalog and the suites under data/admission. A type no environment declares: no_candidate (why: unknown_task_type)
EntryChat   = {kind: "local" | "cloud", name: str}        # claude-desktop, meta-ai = cloud; open-webui+local model, owner panel = local
Task        = {task_id, task_type, sensitivity, entry_chat, created_by, payload_ref, input_refs?: [ref], exclude?: {machine_ids?, environment_ids?}}
```

* `created_by` is `OWNER` or `PLANNER_ACCEPTED` (a planner suggestion the owner accepted; the acceptance is an OWNER event). A suggestion that was not accepted is not a task.
* `payload_ref` is a reference into the coordinator's store (a `CaptureRef`), or `result-of:<task_id>` (section 11); never the content. `input_refs` (optional, additive) lists further inputs in the same forms. The router reads the reference's sensitivity, not the text. Tools take references, not raw content (CLAUDE.md).
* `entry_chat` is set by the adapter that received the request, not by the requester. An adapter that cannot tell says `cloud` (fail closed).

```
Route          = {environment_id, subtype: "provider" | "own_harness" | "embedded" | "local_model",
                  model_id, access_id, machine_id}
RouteDecision  = {task_id, route: Route, rule_trace: [Step], escalation_step: 0|1|2, evidence_label, effective_sensitivity}
Step           = {step: "sensitivity" | "exclusion" | "admission_filter" | "cost_class" | "admission_rate" | "time" | "load" | "tie_break",
                  candidates_in: [route_key], candidates_out: [route_key],
                  values: {route_key: value}, margin?: number, note?: str}
Refusal        = {task_id, code, reason, details}
```

`evidence_label` is `measured here x/10` (a valid admission fact) or `no data`. An unmeasured combination is provisional (it passed the hardware and access checks only; the router holds no public-evidence data yet), and a provisional route never receives `confidential` (R3). `RouteDecision.provisional` is true when the chosen route has no valid admitted measurement; the label stays `no data`.

### Refusal codes (typed; `reason` is the owner-facing English text)

| code | when |
|---|---|
| `confidential_from_cloud_chat` | task is `confidential`, `entry_chat.kind == cloud`. Owner text: "This task is confidential, so it can only be created from the owner panel or a local chat, not from a cloud chat." |
| `sensitivity_missing` | no level on the task or on its payload reference (no default, no guess) |
| `no_candidate` with `details.why = "unknown_task_type"` | no environment declares this task type: zero candidates, nothing invented (the older code `unknown_task_type` is now the detail) |
| `no_candidate` | the catalog, crossed with accesses, installed environments and OS support, is empty; `details` lists each environment with its typed reason (`not_installed`, `no_access`, `not_signed_in`, `unsupported_os`, `closed_by_owner`) |
| `no_local_candidate` | `confidential`: candidates exist, none is local on a paired own machine |
| `provisional_only` | `confidential`: only provisional / `no data` combinations exist (R3) |
| `sandbox_missing` | the environment writes or may run code and the machine has neither the tool's execution-off mode nor a sandbox backend (section 6) |
| `outside_workspace` | an environment tried to write outside the task workspace (raised by dispatch, not by `route`) |
| `automated_use_unreviewed` | commercial environment whose `automated_use` is `to review` (never auto-routed, spec 10.4) |
| `escalation_exhausted` | 2 escalation steps used, or no further candidate that sensitivity allows |
| `no_owner_origin` | `dispatch`: no OWNER event created or approved this task (the operator's event does not count) |
| `weak_grader` | `report_result` with a grader that is not code (model- or human-graded): ignored |
| `payload_missing`, `no_sender`, `dispatch_rejected` | `dispatch`: input not found, no way to send, or the node refused (`details.node_reason` has its typed reason: `stale_recipe`, `busy`, `node_offline`, ...) |
| `no_grader`, `output_missing` | `grade`: no code grader registered for the task (or one that cannot be applied), or no such output for that task |
| `confidential_cloud_environment` | `dispatch`: an input is confidential and the chosen environment is cloud (checked again at dispatch against the stored level of every `result-of` input) |
| `stale_decision` | `report_result` for a decision that is not the task's latest |

## 2. Calls

```
route(task: Task) -> RouteDecision | Refusal
dispatch(decision: RouteDecision) -> TaskHandle | Refusal
report_result(task_id, escalation_step, grader_result: {passed: bool, grader: "code" | "result_verification", evidence_ref}) -> RouteDecision | Refusal | None
filter_outbound(intent, entry_chat) -> Intent        # section 5
```

* `route` is pure over stored facts: the same task, facts and config give the same decision (recomputable, contract 10.6). It sends nothing.
* `dispatch` sends the signed task through the existing channel (section 6 of `ROUTER_ENVIRONMENTS.md`). `TaskHandle = {task_id, decision_ref, machine_id, task_state}` where `task_state` follows contract 3.7 (created ... done|failed|rejected|timed_out|cancelled). No new listening port on any node.
* `report_result` is how a code grader's verdict re-enters. `escalation_step` must be the latest decision's (else `stale_decision`). `passed: true` closes the task and returns `None`. `passed: false` computes the next candidate by the same rule, excluding combinations already tried for this task, with `escalation_step + 1`. Past step 2, or when sensitivity forbids the next candidate: `Refusal(escalation_exhausted)` plus an owner-notice intent. Only a **code grader** may fail a task; a model-graded result is labeled weaker and cannot trigger or cancel an escalation.
* All three calls are idempotent per (`task_id`, `escalation_step`).

## 3. The written rule (`route`)

Order, each step recorded in `rule_trace`:

1. **Sensitivity filter.** `confidential`: local environments on paired own machines only (R1); provider = cloud (6.6); never third-party machines; never provisional (R3). `internal`: own machines + owner-approved providers per the owner policy (data; default as the owner set it). `public`: owner policy only.
1b. **Exclusion** (only when the task carries `exclude`): machines and environments the task must not use are removed, recorded as a step `exclusion` in the trace. It runs after the sensitivity step, so it can only remove candidates; it is never relaxed, it travels with the task, and every escalation honors it. When it leaves nothing, the refusal is the normal one (`no_candidate`; `no_local_candidate` for a confidential task; `provisional_only` when sensitivity itself had left only unmeasured combinations).
2. **Admission filter**: a combination with a valid measurement below the threshold is dropped (`not_admitted`, spec 8.1). Under the policy value `rule.unmeasured = measured_first` (default; the other value is `cost_first`) a combination with no valid measurement is also dropped (`unmeasured`) when another candidate is measured and admitted, whatever its cost class; with nothing measured the cheapest unmeasured one competes and the decision is flagged `provisional` (public and internal only). Then **cost class**: local < API < subscription; keep the lowest class present.
3. **Admission rate** where a measured fact exists (section 4); `no data` ranks below any measured rate within the same class. Differences inside the **tie margin** count as equal. The margin is a visible config value (`rule.tie_margin` in `policy.json`, confirmed at STOP 1 as `0.1` of the pass rate, printed in every trace; it is not a recommendation).
4. **Measured time** (median wall-clock of the last N graded tasks of this type on this combination, fact `route.time`). Not measured: ranks last, labeled.
5. **Fewer tasks taken** on the machine (active leases), minus **shared quota**: environments of one plan share a counter kept by us (`quota.<access_id>`: tasks dispatched in the window). We never read provider usage.
6. **Tie-break**: lexicographic `environment_id`, so the decision is deterministic. Recorded as `tie_break`.

The model never chooses the route, never ranks, never sees the candidates. A planner model may suggest a `task_type` or `sensitivity` to the owner, who accepts it (spec 11.2).

### rule_trace example (illustrative, not a recorded run)

```
task t-41 code / public / entry cloud:claude-desktop
 sensitivity  in 5 -> out 5   (public: owner policy allows provider)
 cost_class   in 5 -> out 2   values {llamacpp@lenovo: local, opencode+llamacpp@lenovo: local, claude-code@mac: subscription, codex@mac: subscription, pi+llamacpp@lenovo: local}   keep local
 admission    in 2 -> out 1   values {opencode+llamacpp@lenovo: 9/10 "measured here 9/10", pi+llamacpp@lenovo: "no data"}  margin 0.1
 time / load / quota  in 1 -> out 1
 decision opencode+llamacpp@lenovo, escalation_step 0, evidence "measured here 9/10"
```

## 4. Admission hook (T4c writes, the router reads)

T4c writes one fact per combination into the coordinator's store (facts carry status and evidence, M1):

```
key:    admission.<task_type>.<machine_id>.<environment_id>.<model_id>.<access_id>
value:  {passed: int, of: int, threshold: 8, suite_hash, tasks: [task_id...],
         graded_by: "code", measured_at, early_stop: bool, label: "measured here 8/10"}
status: tested
```

* Only code-graded tasks count (A1). Model- or human-graded ones may appear under `weak: {...}` and are never read by the router for the rate.
* **Present** = a fact with `status == tested`, `value.graded_by == "code"`, `passed >= threshold`. A fact below the threshold means **not approved**: the combination is dropped from candidates for that task type (spec 8.1), it does not merely rank low.
* **Invalidated** (the router treats the fact as absent, label back to `no data` / provisional) when any of these changes: the model id or its file SHA-256; the environment's pinned commit or version; the machine id; the suite hash; the access id (e.g. plan changed). Freshness by time is **not** used (clock-independent, contract 3.2); `measured_at` is informational.
* Until T4c exists the router runs with no admission facts: every combination is provisional/`no data`, so `confidential` gets `provisional_only` and `public`/`internal` still route, labeled. That is the true state, not a bug.

## 5. Outbound sensitivity filter (T2b adapters and T4b board call it)

```
filter_outbound(intent: Intent, entry_chat: EntryChat) -> Intent
```

`Intent` is the Harness App intent (`kind`, `run_id`, `actor`, `sensitivity`, `payload`, `text_fallback`). Rules, in code:

* `entry_chat.kind == local`: returned unchanged.
* `entry_chat.kind == cloud` and `intent.sensitivity == confidential`: `payload` is replaced by `{task_id, state, machine}` only, `text_fallback` by a generated English line with the same three fields. **No task text, file name, result content or model summary.** The returned intent keeps `sensitivity: confidential` and gains `redacted: true`.
* `cloud` and `internal`: payload passes only through the owner policy's redaction (identifiers, size cap), logged.
* Unknown sensitivity or unknown entry kind: treated as `confidential` / `cloud` (fail closed).
* The function is total and pure; adapters must route every outgoing intent through it (conformance: an adapter path that sends an intent without calling it fails).

## 6. Events (every decision is an event)

| event | actor | content |
|---|---|---|
| `route_decision` | ROUTER (see note) | the `RouteDecision`, candidates and the full trace |
| `route_refusal` | GUARD | the `Refusal` |
| `dispatch` | GUARD | decision ref, machine, task id, adapter, workspace id |
| `escalation` | ROUTER | from, to, grader result ref, step |
| `owner_notice` | GUARD | on `escalation_exhausted` (an intent through `filter_outbound`) |

Owner choices (accepting a planner suggestion, closing a subtype in the draft, approving a provider) stay OWNER events. **Actor tags (decided at STOP 1):** ROUTER for `route_decision` and `escalation`, GUARD for refusals and dispatch.

## 7. Conformance this adds (each with a failing fixture)

* R1: a decision with `confidential` and a provider / cloud / non-paired machine route is rejected.
* R1b: `confidential` + `entry_chat.cloud` task creation is refused (`confidential_from_cloud_chat`).
* R1c: `filter_outbound` result for `cloud` + `confidential` contains no `payload` content.
* R2: a third escalation is refused. R3: provisional combination with `confidential` is refused.
* W-env (W1/W2/W4 for `environment_run`): the coordinator looks up the OWNER origin of the task before it sends; the node authorizes before it accepts and before it runs; only an OWNER-tagged event naming the task counts. Each has a failing fixture.
* X1 (exclude is a hard filter at every step, never relaxed, does not let a confidential task reach a cloud environment), I1 (a result inherits its source's level; unknown source is confidential), G1 (the runner is never called without a sandbox; model- and human-graded never pass), W-env for decompositions (the owner decision is an OWNER-tagged event from a human gesture for exactly that decomposition, and a subtask has an origin only through a recorded decomposition rechecked from that event, matched by id). Each has a failing fixture.
* R4: nothing under the router package imports a module that drives a model or an agent loop (the model never chooses the route).
* L1: every owner-facing string of the router and the task rejections is English ASCII text.
* The placeholders R1-R3 in `conformance/` become real; the ratchet only gets stricter.

## 8. Seams to other sessions

* **T3b draft** (owner-approved subtypes/accesses per machine): read through `router.policy.get(machine_id)` returning `{open_subtypes, open_accesses, internal_policy}`; default `all detected, owner can close`: since node-preview-8 every subtype in the catalog is open by default and the owner's profile (`<state>/coordinator/profile.json`) closes subtypes, environments, accesses or a machine's cloud use (`closed_by_owner`).
* **T3a**: the local llama.cpp endpoint comes from the fact `setup.server.endpoint` of the machine's install run (loopback literal only).
* **T4b**: calls `route`, `report_result`, `filter_outbound`; owns the board, leases and the director/worker pattern. The router does not hold leases; it reads active-lease counts from the board through `board.active_leases(machine_id)`.
* **T4c**: writes section 4 facts.

## 10. Results and grading

```
result_of(handle: TaskHandle) -> {state, output_ref}     # state: the node task's state (contract 3.7); output_ref: a CaptureRef (SHA-256 hex) once state == "done", else null
set_grader(task_id, spec: {kind, value})                 # by whoever created the task; the grader is the task creator's, never a model's
grade(task_id, output_ref) -> {passed, grader: "code", evidence_ref} | Refusal
```

* `result_of` stores the output in the coordinator's output store with **the level the task was routed under**. The reference is stable: the same content of the same task gives the same reference.
* A grader spec is `{kind, value}`. Text kinds run in the coordinator: `equals` (value: the expected text), `contains` (value: the text that must appear), `regex` (value: the pattern, at most 500 characters; one that does not compile is refused), `sha256` (value: the expected hash). `run_tests` (value: the tests) **executes model-written code**, so it runs only through a sandbox runner when a sandbox backend is ready; otherwise `Refusal(sandbox_missing)` and the runner is never called. `model` and `human` kinds never pass: `Refusal(weak_grader)`. The coordinator itself never executes anything (conformance N3 still holds).
* The verdict `{passed, grader: "code", evidence_ref}` is what `report_result` accepts. `evidence_ref` is the SHA-256 of (task, output, grader kind, verdict, runner log hash), so the same inputs give the same evidence.
* The coordinator's runner for `run_tests` is injected (`sandbox_ready`, `sandbox_runner`). The integrated wiring is `envrec_coordinator.admission.router_bridge`: `sandbox_ready` is `sbx.docker_ready`, `sandbox_runner` calls `sbx.run_exam_code` (the stored output is the solution; the spec is `{kind: "run_tests", value: <hidden checks written with the sandbox's `check(actual, expected)` and `raises` helpers>, n_checks: <how many must finish>, timeout_s?: <default 10>}`). A spec without `n_checks`, or an output with no code, fails closed (not passed) without running anything. When no backend is ready `run_tests` answers `sandbox_missing`; a backend that disappears between the check and the run gives the same answer (`SandboxGone`).

## 11. Inputs that are results, and verification

* **Input reference `result-of:<task_id>`**, resolved by the coordinator's output store and accepted by `dispatch` (the output text is the input; with several inputs they are joined under headers `--- input N ---`).
* **Stored instruction references.** `instr:<name>` is a reference to a project-written instruction text in the coordinator's store (`skills/router-environments/references/instructions.json`, the `Router(instructions=...)` seam); `instr:verify` is the verifier's. It is accepted as `payload_ref` or in `input_refs`, resolved at dispatch (an unknown name is `payload_missing`), contains no task data and therefore **never raises a task's level**. A verification is `payload_ref: "instr:verify"`, `input_refs: ["result-of:<task>"]`: the verifier receives the instructions, then `--- input 2 ---`, then the output.
* **A result inherits the level of its source.** A task is routed under the highest of its own level and the levels of all its inputs (`RouteDecision.effective_sensitivity`; the sensitivity step's note says when an input raised it). An input whose source the router does not know is treated as confidential. Because the creation guard uses the effective level, a verification of a confidential result asked from a cloud chat is refused (`confidential_from_cloud_chat`), and a verification asked from a local chat can only go to local, measured environments. `dispatch` checks again against the level stored with each `result-of` input and refuses (`confidential_cloud_environment`) a cloud environment, even if the decision was made under a lower level.
* **Verification is routed under the task_type of the task it verifies** (a verification of a `code` task is a `code` task; of a `text` task, a `text` task). There is no `verify` task type.
* **A director's own task_type must be `code` or `text`** (the only types with a catalog entry and an admission suite). The director is a task like any other; it has no privileged route.
* To keep a verifier off the machine or environment that produced the output, the creator sets `exclude` on the verification task (section 3, step 1b).

## 12. Origin of a director's subtasks

A task is sent only when an OWNER event created or approved it (`no_owner_origin` otherwise). For the subtasks a director makes, the owner takes **one** decision on the exact decomposition, and the coordinator checks it in its own store. The authority is the recorded event; nothing the caller says counts.

```
record_owner_decision(parent_task_id, subtasks=[{id, type, sensitivity}, ...], gesture) -> owner_event_id    # called by the layer that shows the dialog, AFTER the human gesture
approve_decomposition(parent_task_id, subtasks, owner_event_id) -> decomposition_id | Refusal(no_owner_origin)
own_subtask(task_id, decomposition_id, subtask_id) -> owner_event_id | None
```

* **The OWNER event.** `record_owner_decision` writes an event `decomposition_decision`, tagged OWNER, whose `gesture` is one of `os_dialog | host_dialog | panel` (the OS approval dialog, the chat host's own dialog, the owner's panel) and which records the parent and the ordered list of subtasks with their ids, types and levels. A call made by the operator is tagged CLAUDE-CODE; an event written by code is not OWNER.
* **`approve_decomposition`** has no `by` parameter. It finds `owner_event_id` in the store and requires: it exists, it is a `decomposition_decision`, its tag is OWNER, its gesture is one of the three, and the decomposition it records equals the one being approved **character for character** (parent, count, order, and each id, type and level). Otherwise `Refusal(no_owner_origin)` and nothing is recorded. On success it records `decomposition_approved` (GUARD) and **returns the decomposition id** `dec_<20 hex>`, a hash of the exact decomposition and the owner event: the same decision gives the same id, another decision another id.
* **Matching is by subtask id** from the approved list, not by type and level in registration order. Order matching was dropped: two subtasks of the same type and level would have been interchangeable, whichever registered first would have taken the slot, and a retry or a reordered registration would have changed which task the owner's approval covered. With ids, the approval names exactly which task may be which subtask. `own_subtask` takes no type or level from the caller (they are the owner's listed ones); an id not on the list, an unknown decomposition, or an id already held by another task gives `None`. The holder may ask again.
* **The origin is the owner's decision event.** `own_subtask` returns the id of the OWNER event, and that is the origin signed into the node's token. At send time the coordinator re-checks the decision from the store (a `decomposition_approved` record written by code that points at the operator's event, or at nothing, gives no origin), and compares the task actually being sent with the listed one: the type must be equal, and the level must be **at least** the listed level (a task routed under a lower level than the owner listed would be more permissive than approved; a higher one is stricter and fine). `dispatch` refuses with `no_owner_origin` otherwise.
* A subtask outside the list needs a new OWNER decision.

## 13. Actor tags

`ROUTER` is a tag of `Event` and of `derive_tag` (it is in `TAGS`): the event types `route_decision` and `escalation` derive to ROUTER; refusals, dispatches, grades and owner notices stay GUARD; owner events stay OWNER (or CLAUDE-CODE when the operator acted). A stored tag wins over the derived one.

## 9. Facts the router reads (implementation notes)

* `MachineFacts` per paired computer: `installed` (environment ids found), `signed_in` (access ids with a session), `env_names` (variable NAMES present), `sandbox_backends`, `model_endpoint` (loopback literal `host:port`) and `model_id`. The producer is the node's `ai_clis` / `model_runtimes` probes and T3a's `setup.server` fact; until the coordinator adapter exists the tests supply them (`DictFacts`). A signed-in state that has no status command (Muse Code) is stated by the owner. An api-key access is present when all of its `env_names` and at least one of its `env_names_any` exist (Muse Code: `MODEL_API_KEY` or `META_API_KEY`).
* `Policy` comes from `skills/router-environments/references/policy.json` (conservative defaults: only the local model open, no provider approved for internal, no automated use cleared) until T3b supplies the owner's draft.
* The owner's creation of a task is `Coordinator.own_task(task_id)` (event `environment_task_origin`, OWNER). A suggestion by the planner becomes a task only through the same call after the owner accepts it.
