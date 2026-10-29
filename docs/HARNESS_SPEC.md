# What a harness is, and what ours must be

A harness is everything around a model that turns it into an agent. The model does two things only: it reasons, and it chooses. Everything else is the harness: senses (tools), know-how (skills), a view of the world (context), a past (memory), a loop that keeps going, and guard rails. Five parts plus guards, each requirement with an ID. Each ID has at least one executable check in `conformance/` (run `pytest conformance`; the file is named after the ID). A check that fails is a true statement about the code, not a bug in the check.

Five actors appear in every decision, and every event must say which one acted (ROUTER is new; it exists only once the router is built, and it is not a loophole for SCRIPT):

* **MODEL**: the model chose an action from the menu (no fallback involved).
* **GUARD**: code refused or enforced a rule. Legitimate: it never picks what happens next, it only stops what must not happen.
* **SCRIPT**: code decided the next action or the outcome (a forced advance, a fallback action, a fixed sequence, a rule table that picks a route, a repair that rewrites the action). Not legitimate in the missions, because then the model is decoration.
* **OWNER**: the human approved, or typed an answer.
* **ROUTER**: the router chose a harness/model/machine combination for a task, by a written rule, only among combinations that passed the admission exam and that the task's sensitivity allows (`docs/NETWORK_CONTRACT.md` section 10). The event lists the candidates, the rule and the choice. It never chooses the model's actions.

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
* **N5.** The default and configured inference endpoints used by node code are loopback literals (`127.0.0.1` or `::1`; `localhost` is not accepted). (Static check over node code and node-used configuration.) **N5-runtime:** the node checks at runtime that a third-party inference server is bound to loopback and refuses to use it otherwise. (Placeholder, skipped until the node agent exists.)

* **T1.** A remote task can run only what the node's read-only allowlist names. The node's task code (`src/envrec_node/tasks.py`) imports no module that can start a program or load code, calls no dynamic-lookup builtin, and keeps its allowlist as one dict literal. (Static check with a failing fixture: a shell command from a task, dispatch by name.)
* **Clock.** No validation path inside a session or for a discovery answer rejects solely on wall-clock time: freshness is the session id, the sequence number and the query nonce. (Static check with a failing fixture: a session that compares `ts` with the clock.)
* **P1.** No OS-specific import or call (`winreg`, `msvcrt`, `fcntl`, `termios`, `pwd`, `AppKit`, `os.getuid`, `os.chmod`, `sys.platform`, `platform.system()`, a spawned OS command, ...) exists in the network packages (`envrec_net`, `envrec_node`, `envrec_coordinator`, `envrec_mas`) outside the per-OS layer (`envrec_os`). (Static check with a failing fixture.) The legacy `envrec` and `envrec_worker` packages predate the rule and are exempt until they are migrated.
* **H1.** No OS- or tool-specific knowledge (install paths, state folders, service names, registry keys, package-manager or shell command strings for a specific OS) exists as a string in those same packages outside skills and the per-OS layer; it is data in a skill (for example `skills/os-state-paths/references/state-dirs.json`). (Static check with a failing fixture.)
* **Protocols walked by code.** `protocol-pair` and `protocol-node-connect` are enforced state machines like any protocol (`envrec_net.protocol`): a move needs the evidence its rule names, any other move is refused and logged, a step past its `timeout_s` moves to its `on_timeout` state. Their events carry the actor tag GUARD (code enforced or recorded) or OWNER (the owner's yes or no).

## ROUTER (R): which combination gets a task (`docs/NETWORK_CONTRACT.md` section 10, `docs/RECOMMENDATION_SPEC.md`)

* **R1.** A confidential task is never routed to a cloud model, a commercial harness, or a machine that is not one of the owner's paired nodes. (Placeholder, skipped until the router exists.)
* **R2.** Escalation never exceeds 2 steps. (Placeholder.)
* **R3.** A provisional combination (public evidence and hardware fit only, not measured here) never receives a confidential task. (Placeholder.)

## ADMISSION (A): what counts as passing

* **A1.** Only code-graded tasks count toward the admission pass threshold; tasks graded by humans or by a model are labeled weaker and do not count. (Placeholder, skipped until admission exists.)

## VERIFICATION (V): how we know

* **V1.** The verifier is independent: a clean context, `created_by` never equal to the verifier (enforced at the store), and independent evidence means distinct raw captures and channels, not distinct provider names.
* **V2.** Ground truth is independent of the model. The false-verified rate is reported with its denominator; zero over zero is reported as "no evidence", not as safe. Replay and real are never summed.
* **V3.** Completion is verified from the world by code (connected state, playback state, server health with a fresh nonce), never by the model's claim.
* **V4.** Tests assert behavior. No vacuous assertions (`is not None`, `or True`). Every guard has a test that would fail if the guard were removed.

## How to use this

* `pytest conformance` runs the checks. They use fakes and the recorded event logs; they never touch a real device. The one exception is N4, which starts two local processes and talks over loopback on this machine only.
* `python conformance/tools/actor_counts.py` counts, per recorded run, who made each decision.
* `python conformance/tools/menu_truth.py` prints the exact menu the model is offered in every state of every protocol.
* `conformance/mutation_check.sh` removes five guards one at a time in a scratch git worktree and shows which existing test notices.
