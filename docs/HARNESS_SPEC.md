# What a harness is, and what ours must be

A harness is everything around a model that turns it into an agent. The model does two things only: it reasons, and it chooses. Everything else is the harness: senses (tools), know-how (skills), a view of the world (context), a past (memory), a loop that keeps going, and guard rails. Five parts plus guards, each requirement with an ID. Each ID has at least one executable check in `conformance/` (run `pytest conformance`; the file is named after the ID). A check that fails is a true statement about the code, not a bug in the check.

Six actors appear in every decision, and every event must say which one acted (ROUTER is new; it exists only once the router is built, and it is not a loophole for SCRIPT):

* **MODEL**: the model chose an action from the menu (no fallback involved).
* **GUARD**: code refused or enforced a rule. Legitimate: it never picks what happens next, it only stops what must not happen.
* **SCRIPT**: code decided the next action or the outcome (a forced advance, a fallback action, a fixed sequence, a rule table that picks a route, a repair that rewrites the action). Not legitimate in the missions, because then the model is decoration.
* **OWNER**: the human approved, or typed an answer.
* **CLAUDE-CODE**: the operator (the assistant that builds this) acted for the owner in a verification run, with the owner's authorization given in chat. It is counted apart, written into the evidence of what it answered, and never mistaken for OWNER. It is zero in any run the owner drove.
* **ROUTER**: the router chose a harness/model/machine combination for a task, by a written rule, only among combinations that passed the admission exam and that the task's sensitivity allows (`docs/NETWORK_CONTRACT.md` section 10). The event lists the candidates, the rule and the choice. It never chooses the model's actions.

## Harness, not encoder

The failure this project is built to avoid is an *encoder*: a program that already knows the right answer and carries the model along to it, so that the model's "decision" is decoration. A run that ends well is not evidence of a harness. Who decided is.

**Encoder (forbidden).** The outcome is decided in advance and forced. That can be:
* an `if` or a table that picks the next tool, or the next protocol step, for the model;
* code that calls a tool on the model's behalf, advances the protocol for it, or declares success for it;
* wording of the view, the menu or a skill that has been tuned, by trial against one model, until that model reliably picks the action we wanted. The code is innocent and the text is the encoder; the log will say MODEL for a choice the text made.

**Harness (required).**
* Code is GUARD: it validates, blocks what is invalid or out of order, executes what the model chose, records everything, and is the only thing that can say "the protocol has not ended". It never picks.
* Knowledge lives in skills, protocols (`SKILL.md` and their data files) and the per-OS layer (`envrec_os`), not in Python branches.
* The view tells the truth: what is known and how well, what is missing, which registered tool says it produces the missing thing, and why something was refused (a typed reason). The model decides what to do with that.
* Every decision carries its actor: MODEL, GUARD, SCRIPT, OWNER. CLAUDE-CODE steps in a verification run are zero.

**The fine line.** Improving what the model sees is allowed, and is most of the work. A change to the view, the menu or a skill text passes only if all of these hold:
1. *Generic.* It is built from the protocol spec and the tool specs (a missing key, its declared producers, a refusal reason), and is valid for any step, any protocol, any key. It names no tool, step or key written into the code for the sake of one run.
2. *Neutral to the answer.* It would still be correct if the right answer were a different tool. It points at facts, not at an action.
3. *Held on other views.* It was checked on other runs and other views, not only on the one it was tuned on, and by what the log shows, not by one model's one lucky reply.
4. *True.* It never states something false: not completion before a tool has run, not "nothing more is needed" when the guard would still allow more, not a rule nothing enforces.

**Short protocols, closed by a terminal action (the machine analysis, 2026-10-30).** A protocol that needs the model to take a pure formality ("advance") after its real work invites a stall that no wording should be tuned to cure. Instead the protocol is cut at its natural end. A step may list `close_on` tools in its skill data: when the MODEL chose one of them, it succeeded, and the evidence for an exit is in, the GUARD takes that exit and records it; the closing tool itself is offered only once the rest of an exit's evidence is in, which the loop reads from the same spec (no tool names in code) (`protocol_state` by `evidence`, tag GUARD, with `closed_by`). That is validating a terminal action; the choice of the action, its arguments and its meaning stay with the model. `protocol-analyze` is one step that ends on `finish_analysis` and stores the analysis; "which AI do you want here" is a different protocol, `protocol-setup-options`, which refuses to start without a stored analysis and reads it instead of probing again. Each has its own skill, a small menu and its own typed end. The loop moves a protocol in exactly three ways: the model's `advance`, the guard closing a step on a tool the model chose, and the experimental dial that the shipped profiles leave off (every move it makes is SCRIPT); a test fails if a fourth appears.

**How to tell a fix is an encoder in disguise.** Ask: if I ran the same wording on a stronger model, a weaker one, and a view from another step, would it still be right? Did I choose the wording by trying variants until the model produced the action I wanted? Would the text read differently if the correct next action were another tool? Does the log now tag as MODEL a choice that the text all but dictated? Any "yes, I tuned it" is a failed check.

*Example (2026-10-30, Phase 2 step 1).* A local 9B model kept replying `done` or repeating `record_finding` and never ran `node_memory_fit`. Legitimate fixes: the view says "to move on it needs `machine.model_path`, produced by `node_memory_fit`" (built from the protocol's `needs` and the tools' `produces`), the `done` refusal is typed and names the missing key, required arguments are shown in the menu, and the capped menu keeps the producer of a missing key inside the cap. Not legitimate: rewording the step goal, the way-forward line and adding an extra "evidence is in" line, and replaying one view until the model picked `advance` 8 times out of 8. That was caught and all three were reverted; what the model does after that is its own, or a typed stop, and the log says which.

Every report that asks for approval states a **harness check: passed / why** for the changes it contains. Before touching agent, protocol, view, menu or skill code, re-read this section.

**The draft and the hard limits (protocol-draft, 2026-10-30).** "Draft first" has the model propose the whole setup of the network, and the project also has hard limits that no model may cross. They do not conflict, because they act at different layers:
* *GUARD (code), never the model's:* the machine and the access exist (a tool counts only with a sign-in found, an API key only when its variable was found, never a value), a local model fits the available memory that was measured, its license class is allowed, a cloud access is not given to a machine the owner marked confidential, a topology has the machines and director it needs, a chain lists every machine, nothing the owner left out is used, and what the owner STATED this round (an edit or an answer) is not contradicted. A violation is a typed refusal the model can read (`access_not_detected`, `model_not_allowed`, `confidential_cloud`, `contradicts_owner`, `assumption_unmarked`, ...); no fact is written. The limits are the same functions the setup options already use (`options.candidates`, `apply_filters`), not a second copy.
* *MODEL (inside the limits):* which subtype, which access, which model among those that fit, resources, tasks, topology, director, freedom, rhythm, and when to ask. Resources the owner has not stated are a bias the model weighs; resources the owner stated bind. The owner profile (earlier corrections) is shown as context and binds nothing: a draft that ignores it is accepted.
* *Assumptions:* an element of the draft is the OWNER's (stated, with its source) or an ASSUMPTION that names what it rests on (a fact key that exists, an `owner.*` key or a listed `default:<id>`). Code checks that the reference exists; it cannot check that it supports the claim, and says so. Measured things (an access was detected, a model fits) are recorded as `checks`, written by code. There is no third kind.
* *The owner:* a correction is an OWNER event with the owner's words; the model turns it into typed edits, and the guard checks that each quote is in those words. Approving is the owner's click, an OWNER event. The operator doing either in a verification run is CLAUDE-CODE.
* *Task-level routing stays with the written rule* (contract section 10), later. The draft assigns environments, not tasks to environments, and every proposal is "provisional, not measured here": a confidential task goes only to a combination measured here.
* *Moving the protocol:* only the model's `advance`, the guard closing on `finish_draft` / `load_network` (when the model chose it and the exit's evidence is in), and the dial that stays off. The draft's evidence facts (`draft.machines_covered`, `draft.finished`) are the guard's records of what the model's own tool calls produced.

## LOOP (L): the cycle that keeps going

* **L1.** One action per turn, validated against the Action contract. If it is invalid, one retry carries the exact error text. A narrow repair layer is allowed only if every repair is logged and counted and it never changes which tool was chosen.
* **L2.** The model chooses from a closed menu. The menu is generated from the tool registry and filtered by target kind, ring, platform, protocol state and policy. A tool outside the menu never executes.
* **L3.** Code never chooses the next tool for the model; the model chooses its own action every turn. (The router may choose the harness/model/machine combination for a task, under ROUTER above; that is not a choice of the model's action.) A protocol state may change in code only after its evidence has been produced by an action the model chose, and the next action is still the model's. Any "walk for the model" dial must be off in the missions; where it is on anywhere, every forced step is tagged SCRIPT and counted.
* **L4.** Stops: `done` is accepted only when the protocol has ended (otherwise it is refused, however many times it is asked); a step cap; a time cap; a no-progress guard (the same action again with no new fact); a repeated `ask_human` is rejected; with no human available the run ends as `blocked_needs_human`.
* **L5.** Every turn writes an event with: a digest of the view, the menu ids, the raw model reply, the validation result, the policy decision, the tool result and fact ids, the model and profile, and the actor tag (MODEL, GUARD, SCRIPT, OWNER).

## TOOLS (T): the senses and hands

* **T1.** Tools are generic: no device names, no fixture names. One registration point. A fixed docstring format: a summary of at most 12 words, Args, Returns, Tier, Platforms, Notes. A provenance field (builtin or generated).
* **T2.** Arguments are typed and validated before anything runs. Results are `ProbeResult` with Evidence (provider, a raw reference by sha256, limitations). Raw output never enters the prompt.
* **T3.** Tiers are enforced. Anything that has an effect in the world needs a fresh owner approval that shows the exact action; an undo is recorded before the write; a denial changes nothing.
* **T4.** The privacy gate runs once, at the fact boundary. No tool or skill can bypass it.
* **T5.** The tools a mission needs exist and appear in the menu in the right protocol state.

## SKILLS (S): know-how

* **S1.** Skills are standard `SKILL.md` folders. The description starts with "Use when" and does not summarize the workflow. Disclosure is progressive (the menu shows only name and description). Scripts run only in the sandbox, which fails closed. Tests come from a real capture plus a must-not-match case. A generated skill is kept only after it passes its tests, beats the baseline and passes a clean-context verifier; the owner approves the exact script by its hash before it runs on a real device.
* **S2.** Knowledge lives in skills and protocols, not in per-device Python branches. Generic public tables (usb.ids, OUI, Bluetooth numbers, IANA) are allowed.
* **S3.** Protocols are enforced state machines: states, the tool tags allowed in each state, and the evidence needed to move on.

## CONTEXT (C): what the model sees

* **C1.** The view is budgeted and ranked, includes the unknowns, and never contains raw output or images. Strings that come from the world (hostnames, banners, names) are sanitized and never placed where they could read as an instruction.
* **C2.** The model gets the goal text, the protocol state, the menu and the view. Ground truth and expected answers are never visible to it.

## MEMORY (M): the past

* **M1.** Events are append-only; facts carry a status and evidence; raw captures are kept by hash.
* **M2.** The skills library is the long-term memory: the same signature the second time is handled without a model call; a skill is re-verified on each new device; verified-run counts are kept.
* **M3.** Nothing personal is remembered: ambient ids are hashed, no names, no credentials.

## GUARDS (G)

* **G1.** Authorization is `targets.yaml` OR a pairing the owner confirmed on the coordinator (`docs/NETWORK_CONTRACT.md` section 2); nothing else. Discovery only proposes. A scope guard bounds networks. Enrollment is interactive and the only writer of `targets.yaml`; the coordinator's pairing store is written only by a pairing the owner confirmed. Approvals are fresh. An undo exists. A node or worker has no code that deploys to, enrolls or pushes tasks to another machine (it may inspect itself and run what the coordinator leased to it, within the task's capability token). The sandbox fails closed. There are no credentials anywhere.

## NETWORK (N): the shape of the network (`docs/NETWORK_CONTRACT.md`)

* **N1.** Node code opens no listening socket: it never binds, listens or uses a datagram endpoint. Its one exception is the short-lived ephemeral UDP socket of discovery (`docs/NETWORK_CONTRACT.md` 1.8), created by sending, only in its discovery module. Node code is `src/envrec_node/` and the legacy `src/envrec_worker/`. (Static check.)
* **N2.** Coordinator code (`src/envrec_coordinator/`) opens exactly one LAN-facing listening socket (its bind host is not a loopback literal in the code) at most one UDP discovery site, and at most one more TCP site whose bind host is a loopback literal: the owner's web screen (`docs/NETWORK_CONTRACT.md` 1.7). A second socket that is not a loopback literal fails, whatever it serves. (Static check with failing fixtures: a web screen bound to `0.0.0.0`, to a host the code does not pin, and a second loopback socket.)
* **N3.** Model-written code is never executed except through a sandbox gateway that refuses before starting anything when no backend is ready; no dynamic execution (`exec`, `eval`, `compile`, `runpy`, loading code from a path) exists in `src/` outside a reviewed allow-list. (Static check.)
* **N4.** At runtime, a node process holds zero listening sockets reachable from the network and the coordinator holds one, plus one loopback socket when started with `--web` and one UDP discovery socket unless `--no-discovery`; a discovering node holds one ephemeral UDP socket during the window and none after (exactly one socket at the configured node address; any other must be loopback and at most one). Runs two real processes on this machine over loopback, paired through the `mas` commands, with heartbeats flowing (label SIMULATED-LOCAL); a fixture process that listens must be seen.
* **N5.** The default and configured inference endpoints used by node code are loopback literals (`127.0.0.1` or `::1`; `localhost` is not accepted). (Static check over node code and node-used configuration.) **N5-runtime:** the node checks at runtime that a third-party inference server is bound to loopback and refuses to use it otherwise: after the start it lists the sockets the server process really listens on and stops it if any is not loopback (SIMULATED-LOCAL test with a stand-in server).

* **T1.** A remote task can run only what the node's read-only allowlist names. The node's task code (`src/envrec_node/tasks.py`) imports no module that can start a program or load code, calls no dynamic-lookup builtin, and keeps its allowlist as one dict literal. (Static check with a failing fixture: a shell command from a task, dispatch by name.)
* **W1.** The coordinator sends the one write task (`setup_step`) only through `start_setup_step`, which looks up the owner's approval of that exact plan hash, step and action BEFORE anything is sent and returns when there is none; the generic `start` refuses the write types. (Static check with failing fixtures: a send without the lookup, a send before it, a generic start that sends writes.)
* **W2.** The node authorizes a `setup_step` (plan hash against the signed token, the owner's approval named in it, its own copy of the recipe, the paths the step declares) before it accepts it and again before it runs it; `run_step` is called from `_setup_step` and nowhere else. (Static check with failing fixtures, and runtime tests that refuse each failed check with its typed reason.)
* **W3.** The read-only task types stay read-only: no read-only handler reaches the setup module, and no read-only parameter model has a field a write needs (`plan`, `step`, `action`, `writes`, `scope`, `approval`, `command`, `path`). (Static check with failing fixtures.)
* **W4.** What counts as the owner's approval: an event of the kind `setup_step_approved`, tagged OWNER (never CLAUDE-CODE or GUARD), whose plan hash, step and action match. (Static check with a failing fixture.)
* **W5-W8.** The credential change (`docs/NETWORK_CONTRACT.md` 3.10): W5 the coordinator sends it only after it found the owner's confirmation of that exact plan; W6 the node checks the signed scope before it touches the store and only `_credential_step` applies a change; W7 the value is never passed to a log, a record or the end of a task; W8 the credential module opens no file, prints nothing and writes no environment variable (no plaintext fallback). (Static checks with failing fixtures, `conformance/test_credentials.py`.)
* **Clock.** No validation path inside a session or for a discovery answer rejects solely on wall-clock time: freshness is the session id, the sequence number and the query nonce. (Static check with a failing fixture: a session that compares `ts` with the clock.)
* **P1.** No OS-specific import or call (`winreg`, `msvcrt`, `fcntl`, `termios`, `pwd`, `AppKit`, `os.getuid`, `os.chmod`, `sys.platform`, `platform.system()`, a spawned OS command, ...) exists in the network packages (`envrec_net`, `envrec_node`, `envrec_coordinator`, `envrec_mas`) outside the per-OS layer (`envrec_os`). (Static check with a failing fixture.) The legacy `envrec` and `envrec_worker` packages predate the rule and are exempt until they are migrated.
* **H1.** No OS- or tool-specific knowledge (install paths, state folders, service names, registry keys, package-manager or shell command strings for a specific OS) exists as a string in those same packages outside skills and the per-OS layer; it is data in a skill (for example `skills/os-state-paths/references/state-dirs.json`). (Static check with a failing fixture.)
* **Protocols walked by code.** `protocol-pair` and `protocol-node-connect` are enforced state machines like any protocol (`envrec_net.protocol`): a move needs the evidence its rule names, any other move is refused and logged, a step past its `timeout_s` moves to its `on_timeout` state. Their events carry the actor tag GUARD (code enforced or recorded) or OWNER (the owner's yes or no).

## ROUTER (R): which combination gets a task (`docs/NETWORK_CONTRACT.md` section 10, `docs/RECOMMENDATION_SPEC.md`)

* **R1.** A confidential task is never routed to a cloud model, a commercial harness, or a machine that is not one of the owner's paired nodes, and is never created from a cloud chat. (Driven over scenarios; a router with its sensitivity step removed must fail.)
* **R2.** Escalation never exceeds 2 steps. (Driven; a router allowing a third must fail.)
* **R3.** A provisional combination (public evidence and hardware fit only, not measured here) never receives a confidential task. (Driven; a router that treats an unmeasured one as measured must fail.)
* **R4.** The router imports nothing that drives a model or an agent loop: the model never chooses the route. **X1.** A task's `exclude` is a hard filter that is never relaxed. **I1.** A result inherits the level of its source. **G1.** Model-written code is graded only inside our sandbox; a model-graded result never passes. **Wenv.** The environment run is sent only after an OWNER event for that task, authorized by the node before it is accepted and before it runs, and counted only when tagged OWNER. **L1.** Owner-facing strings are English.

## ADMISSION (A): what counts as passing

* **A1.** Only code-graded tasks count toward the admission pass threshold; tasks graded by humans or by a model are labeled weaker and do not count. (Placeholder, skipped until admission exists.)

## VERIFICATION (V): how we know

* **V1.** The verifier is independent: a clean context, `created_by` never equal to the verifier (enforced at the store), and independent evidence means distinct raw captures and channels, not distinct provider names.
* **V2.** Ground truth is independent of the model. The false-verified rate is reported with its denominator; zero over zero is reported as "no evidence", not as safe. Replay and real are never summed.
* **V3.** Completion is verified from the world by code (connected state, playback state, server health with a fresh nonce), never by the model's claim.
* **V4.** Tests assert behavior. No vacuous assertions (`is not None`, `or True`). Every guard has a test that would fail if the guard were removed.

## HARNESS APP (HA): the owner's chats (`docs/HARNESS_APP_STANDARD.md`)

* **HA1.** Adapters (`envrec_harness/mcp_server.py`, `openwebui_pipe.py`, `client.py`) contain no decision logic: they import no coordinator or OS module, run no program, and name no sensitivity, actor or approval. (Static check with a failing fixture.)
* **HA2.** A channel with a model in the middle never lists a confirmation a model could press (`panel_click`, or the chat's own dialog). Runtime: a tool call from every channel fails to approve a write. (Static check with a failing fixture, plus runtime.)
* **HA3.** Nothing `confidential` reaches a cloud channel, in any intent kind. (Runtime.)
* **HA4.** Every intent has a text fallback, and the core builds intents only through the validating door. (Runtime and static.)
* **HA5.** The card layer names no URL and makes no network call. (Static check with a failing fixture; rendered cards are also scanned in `tests/test_harness_cards.py`.)
* **HA7.** The MCP view's JavaScript is executed, not only read: a host simulator following the published protocol runs it, and the handshake, a non-zero reported height, the painted card and the absence of errors are checked. (Runtime, Node; skipped as "not measured" where Node is missing.)
* **HA8.** A setup step that writes (run or undo, also in a later owner-initiated undo run) is approved as the owner's only with a human gesture (the panel, the system dialog the coordinator raises, the chat's own dialog): an approval that only says "owner" is refused and the decision stays open, and the operator's answer is recorded apart and never runs a step. (Runtime.)
* **HA9.** The text a human approves is the command that runs, on every host. A plan's placeholder `<mas>` is shown as `MAS_HOME` everywhere (a chat strips angle-bracket words as tags: `rm -rf <mas>/downloads/x` reached an owner as `rm -rf /downloads/x`); the mapping is reversible, a confirmation whose command, undo or summary still holds angle brackets is refused (fail closed), and an undo labels its paths "Removes", never "Writes to". (Runtime, per host: chat text, Open WebUI dialog, Claude Desktop form, system dialog, owner panel.)
* **HA10.** Connecting a chat host is a setup like any other: a plan built from data (`skills/connect-recipes`), shown first as text with its exact commands, what each step writes, its undo and the plan hash; each step approved by a human gesture bound to the plan hash; recorded and undoable in a new run. The steps never read out, touch or show another entry of a settings file, back the file up first, refuse with a typed reason what the tool's documentation does not cover (an operating system, a settings file with comments, an app that is running), and the tests run only against a temporary home with fake commands. (Static check of the recipes, plus runtime.)
* **HA6.** Capability detection degrades cleanly: nothing declared means no elicitation is assumed, unknown capabilities are ignored, and the owner panel is always the last resort. (Runtime.)

## How to use this

* `pytest conformance` runs the checks. They use fakes and the recorded event logs; they never touch a real device. The one exception is N4, which starts two local processes and talks over loopback on this machine only.
* `python conformance/tools/actor_counts.py` counts, per recorded run, who made each decision.
* `python conformance/tools/menu_truth.py` prints the exact menu the model is offered in every state of every protocol.
* `conformance/mutation_check.sh` removes five guards one at a time in a scratch git worktree and shows which existing test notices.
