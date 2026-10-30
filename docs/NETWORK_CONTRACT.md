# Network contract (Multi Auto Setup)

Status: normative for everything built from Phase 1 on (revision 2). The catalog, evidence, admission and recommendation rules are in [docs/RECOMMENDATION_SPEC.md](RECOMMENDATION_SPEC.md). MUST / MUST NOT / MAY are used in the RFC 2119 sense. Nothing in this document is implemented yet; section 12 lists what Phase 1 builds. Where this document and an older document disagree, this one wins for the network; `docs/HARNESS_SPEC.md` is amended to match it.

The product: a private network of agents across the owner's own computers. Each machine installs the software once, with the owner's consent, then discovers itself, pairs once with owner permission, gets a workspace set up, and takes part in shared work through a board. A router and sensitivity levels are enforced in code.

Hard limits that this contract never relaxes: own or authorized devices only; read-only by default; every write on a device needs a fresh owner approval that shows the exact action, with an undo recorded first; pairing, Trust prompts and passwords are never bypassed; no credentials stored in the repo, logs, facts or prompts; microphone off; no scanning of other people's systems; the OS firewall and network settings are never changed automatically.

## 1. Roles

1.1 There is one **coordinator** per network. It MUST open exactly **one LAN-facing listening socket**, the one nodes dial. That is the only listening socket of the whole system that another computer can reach. The only other sockets it may open are the owner's web screen of 1.7, bound to loopback, and the optional UDP discovery socket of 1.8.

1.2 A **node** MUST NOT listen on any port that another host can reach. Every connection is initiated by the node toward the coordinator, and replies travel on that connection. Nodes MUST NOT connect to each other.

1.3 **Our node agent opens zero listening sockets** (conformance N1). Its one allowed exception is the short-lived ephemeral UDP socket of 1.8, which only sends the node's own discovery query and receives the answers to it. A **third-party** local inference server (for example Ollama or llama.cpp) is allowed on a node only if it is bound to loopback (127.0.0.1 or ::1). The node MUST verify the binding at runtime; if the server is bound to any non-loopback address, the node MUST warn the owner and MUST NOT use it. The node MAY start a local inference server only with the owner's approval, on loopback only, and MAY stop it after the task. A node never exposes, forwards or tunnels it. The default and configured inference endpoints used by node code MUST be loopback hosts, written as the literals `127.0.0.1` or `::1` (conformance N5; the name `localhost` is not accepted because it depends on the machine's name resolution). The runtime check is N5-runtime (placeholder until the node agent exists).

1.4 The coordinator is itself also a node for work purposes (it may run a local model and a workspace), but its coordinator duties and its node duties use separate code paths.

1.5 The earlier design, in which the controller connects inbound to an SSH server on the device (port 22, `authorized_keys`, a firewall rule created by a `.cmd` file, an SSH port-forward to the device's model server), is **retired**. Its code stays in the repository until Phase 1 replaces it; no new code may depend on it, and nothing under node or coordinator code may use it.

1.6 Code layout, used by the conformance checks: node code lives in `src/envrec_node/`; coordinator code in `src/envrec_coordinator/`; code shared by both (envelope, signing, hash chain; no server code) in `src/envrec_net/`. The legacy `src/envrec_worker/` is treated as node code.

1.7 **The owner's web screen.** The coordinator MAY open ONE additional listening socket that serves the owner's local web screen (machines, pairing). It is **off by default** (`mas coordinator start --web`). It MUST be bound to loopback only (`127.0.0.1` or `::1`), never to a LAN, link-local, multicast or wildcard address, and the address is written in the code, not configurable. Nodes still open zero sockets, and nothing else in the system may open a second one. The screen MUST: serve only plain files from the coordinator process (no CDN, no remote font, nothing fetched at runtime); answer every request only with a session made from a **one-time link** the coordinator prints on the owner's terminal (otherwise 401); accept only the Host names `127.0.0.1:<port>` and `localhost:<port>` (otherwise 403, against DNS rebinding); accept state-changing requests only as POST with a matching Origin and a per-session CSRF token (otherwise 403); never write the link, a session id or a CSRF token to a log, an event or a file. It adds no protocol logic: the owner's yes or no goes through `protocol-pair` exactly as at the terminal, and is logged with the actor OWNER. Conformance: N2 (static: exactly one site whose bind host is not a loopback literal, at most one site that is) and N4 (runtime: exactly one socket at the node address, at most one other, which is loopback).

1.8 **Discovery.** A computer with the program installed can find a coordinator on its own network without anyone typing an address. A node sends one small versioned query (JSON, at most 256 bytes) from an ephemeral UDP port to the directed-broadcast address of each private or link-local IPv4 interface that is up and to 255.255.255.255; virtual interfaces (VPN, containers, virtual machines; names are data in the `net-interface-kinds` skill) are skipped unless the owner passes `--include-virtual`. Nothing is ever sent to a public address. The coordinator answers each valid query by unicast with exactly: its name, its LAN address on the interface that faces the asker, its TCP port, its identity fingerprint, the protocol version and the query's nonce, in a signed envelope; it reveals nothing else.
- **Sockets.** The coordinator MAY open ONE UDP discovery socket (default port 7444, `--discovery-port`, `--no-discovery` turns it off). The node MAY open ONE UDP socket, created by sending (so the operating system gives it an ephemeral port), never bound to a port of its own, used only to send its own query and receive the answers to it, and closed when the discovery window ends; it never answers anyone and never listens afterwards. Conformance: N1 (static: a node never binds, listens or uses an asyncio datagram endpoint; its UDP socket exists only in its discovery module), N2 (static: at most one UDP site in the coordinator), N4 (runtime: the coordinator holds one UDP socket more; a discovering node holds exactly one ephemeral UDP socket during the window and none after).
- **Wildcard bind, and why.** Receiving a broadcast needs a socket that is not tied to one interface address on macOS and Windows (and on Linux for the limited broadcast), so the coordinator's UDP socket binds to every interface. The filtering is done in code instead: a query is answered only when its source is a private or link-local IPv4 address inside the subnet of one of the coordinator's own non-virtual interfaces, it is a well-formed version-1 query, and the source (5 per 10 s) and the whole socket (60 per 10 s) are under a rate limit; everything else is dropped without an answer.
- **Trust.** Before pairing an answer is only a hint (the node has no key to check it with): pairing itself is protected by the key exchange and the owner's approval. After pairing the node accepts only an answer signed by its pinned coordinator key, so an unsigned or differently signed answer is ignored; this also lets a paired node find the coordinator again when its address changes. Several coordinators may answer: the node lists them and the OWNER picks one; a node never joins silently.
- **Waiting.** A picked node dials the coordinator without a certificate and registers as *waiting* with a pre-pairing hello carrying only its name, operating system, architecture and the fingerprint it claims. Waiting entries live while their connection lives and at most ten minutes, are capped in number (8, at most 2 per source) and rate-limited, and nothing else is accepted from an unpaired computer. The OWNER picks a waiting machine on the coordinator (web screen or `mas pair --node`); only then does a 6-digit code exist, bound to that machine's own connection. The code, the key exchange and the owner's approval are as in section 2.
- **When nothing answers** the node says why in plain words and what the owner can check in their own operating system's words (data in the `discovery-guidance` skill): the Windows network profile is Public, a firewall blocks the coordinator's UDP port, the router isolates Wi-Fi clients, a different subnet or a VPN. The software changes no firewall, network profile or router setting itself. `--coordinator HOST:PORT` is the manual fallback.

## 2. Pairing

2.1 A node joins once, with the owner present. The coordinator shows a **6-digit code**; the owner confirms the pairing **once, on the coordinator**, after seeing the node's reported name and operating system.

2.2 The code MUST come out of an **authenticated key exchange in which both sides contribute** (a PAKE with the code as the password, or a short-authentication-string protocol with commitments), so the code cannot be ground offline: an attacker who sees every packet on the network learns nothing that lets them test guesses without talking to a live peer, and a man in the middle succeeds with probability at most about one in a million per attempt and is visible to the owner as a failed pairing.

2.3 Each code allows **one attempt**. It expires after a short time (at most 2 minutes). Attempts are rate-limited per source and globally. A failed attempt invalidates the code and is written to the event log. The code MUST NOT appear in any event, log, fact, prompt or file.

2.4 Pairing yields **long-term identity keys for both sides** (Ed25519). The coordinator stores each paired node's public key; each node stores the coordinator's public key. Nothing else authorizes a node.

2.5 The old pairing code is **retired**: a six-digit number computed as the first four bytes of SHA-256 of the device's SSH ed25519 host key, modulo 1,000,000. It is a public function of a public key and carries about 20 bits, so an attacker on the LAN who sees the host key can generate keys until one produces the same six digits (about a million tries) and impersonate the device. It MUST NOT be used for any new pairing.

2.6 Unpairing removes the key on both sides and is an owner action. A node whose key was removed is refused at the channel.

## 3. Channel and messages

3.1 **Transport.** Mutual TLS (TLS 1.3) from node to coordinator, where each side presents a self-signed certificate whose public key is the identity key pinned at pairing. Verification is by pinned key, never by name or CA. QUIC is a later option and is not required.

3.2 **Envelope.** Every message is an envelope with: `version`, `sender` (identity key id), `type`, `payload`, `seq`, `ts`, and `signature` (Ed25519 over the canonical encoding of the other fields). A receiver MUST reject, with a logged reason: an unsigned message, a bad signature, an unknown version, an unknown type, a sender that is not paired, a message of another session (`sid`), a replayed or out-of-order `seq`, and a repeated envelope nonce. **It MUST NOT reject a message solely because of its timestamp**: inside a session, freshness comes from 3.6, never from anybody's wall clock (conformance, clock check).

3.3 **Capability token.** A task is leased together with a token signed by the coordinator: task id, node id, the actions and tools the task may use, the paths it may touch (inside the workspace), the highest sensitivity it may handle, an expiry, a nonce. The node MUST check the token before every action and refuse anything outside it; the coordinator MUST check results against it. A task can do only what its token grants.

3.4 **Event log.** Events are append-only and **hash-chained**: each event carries the hash of the previous event and its own hash over the canonical event and that previous hash. Verification walks the chain and reports the first broken link. Each side keeps its own log; the coordinator's log is the record of the network. Every event keeps its actor tag (MODEL, GUARD, SCRIPT, OWNER, and ROUTER, see 10.6). Door/link events that were only printed in the earlier design MUST be persisted.

3.6 **Freshness without clocks.** After the pinned TLS handshake the coordinator sends a signed `challenge` with a random nonce, which is the **session id**. The node answers with a signed `hello` that carries that nonce, the session protocol version and its own clock reading. From then on every envelope in both directions carries the session id (`sid`) and a counter (`seq`) that only grows (1, 2, 3, ...), both covered by the signature. A receiver rejects, with a typed reason: another session's envelope (`wrong_session`, a replay from an earlier session), a repeated or old `seq` (`replayed_sequence`), a repeated envelope nonce (`replayed_nonce`), an unsigned or badly signed one, a sender other than the connection's, an unknown type. The `ts` field is informational. Replay protection is as strong as with a clock window and needs no clock. Discovery answers (outside a session) take their freshness from the query's nonce, which the answer must echo.
- **Skew only warns.** The coordinator measures `node clock - coordinator clock` at `hello` (the coordinator's clock is the reference; the node measures the offset too and uses it for its own log times). Above a threshold (5 minutes, configurable) it is logged as GUARD (`clock_skew`), shown on the node's terminal, in `mas nodes --detail` and on the web screen ("the clock of <node> differs by N min"). It never blocks a connection and nothing in a session is refused for it. The software never changes a clock.
- **Typed rejections.** Before it closes a connection the coordinator sends the node a signed `reject` with a typed reason (`replayed_sequence`, `replayed_nonce`, `wrong_session`, `bad_signature`, `wrong_sender`, `challenge_failed`, `protocol_version`, `unknown_type`, `unknown_node`, `revoked`), and logs it. The node prints every refusal and every failed connection attempt in plain words (what happened and what the owner can check); retries are never silent.

3.7 **Tasks (protocol-task).** The coordinator may ask a connected node to run a read-only task, inside that node's session: `task` (task id, type, parameters, capability token, timeout), `cancel`; the node answers `task_update` (`accepted`, `running`, `rejected` with a typed reason) and `result` (task id, status, typed result, evidence, duration), all session envelopes.
- **Token** (3.3, narrowed for this version): signed by the coordinator's identity key; names the task, the one node, the one session, the tools allowed (the task type), single use, and a time to live that the node measures on its own monotonic clock from receipt. The node checks signature, node, session, task, tool and single use before it runs anything, and the time to live when it starts.
- **Allowlist.** A node runs ONLY the tool types in its read-only allowlist, one dictionary literal in `envrec_node/tasks.py` (today eight types: `node_report`, `machine_analysis`, and the six read-only probes `os_details`, `installed_programs`, `gpus`, `model_runtimes`, `ai_clis`, `memory_for_models`). Anything else, a write, a shell command or any other name, is rejected with `tool_not_allowed`; parameters are validated by a strict model that forbids undeclared fields (`bad_parameters`). A task that changes the machine needs the owner's fresh approval ON the node, which this version does not offer. Conformance T1: the node's task code imports no module that can start a program or load code, calls no dynamic-lookup builtin, and its allowlist is that one literal.
- **States:** created, sent, accepted, running, then exactly one of done, failed, rejected, timed_out, cancelled; illegal moves are refused and logged. **Offline node:** the task fails at once with `node_offline`; tasks are NOT queued (a queued task would run in a session its token was not made for). **Concurrency:** at most 2 per node (configurable), enforced on both sides (`busy`). **Timeout:** the coordinator marks the task `timed_out`, tells the node to cancel, and ignores a late result. **Cancel:** by the owner; a task already running in its worker finishes on its own and its result is dropped. **Known limit:** a running task cannot be killed, only its result dropped; that is acceptable for read-only tasks, but before any task that changes a machine exists, tasks MUST run in their own killable process. Every task and result is an event, tagged OWNER (the owner asked) or GUARD (code asked).
- **First two tasks.** `node_report` (sent on connect and every five minutes): the operating system, the node process's own listening TCP and UDP sockets (expected none), the identity key file's location and whether only the owner user can read it (read through the per-OS layer: permission bits, or an access control list on Windows); alerts (listening ports, key readable by others, large clock skew) are logged as GUARD and shown. `machine_analysis` (on the owner's request): the repository's existing discovery run on the node (Ring 0 probes everywhere; the observe-tier discovery skills where the system is supported; the workspace sizing table), grouped as system, hardware, programs, AI available; API keys are reported by environment variable NAME only, never by value; no token is read.

3.8 **Harness runs on the coordinator (protocol-analyze, protocol-setup-options, protocol-lan).** The owner's "Analizar esta máquina" starts a run in which a MODEL chooses one action per turn from a closed menu; code (GUARD) validates it, runs it and records it. Nothing here widens 3.7: the probes are the six read-only task types, sent through the same signed, single-use, per-node tasks, and the model never receives raw output, only evidence-tagged facts (`machine.*`, with the status and the limits of each). API keys appear only as variable NAMES.
- **protocol-analyze** (target kind `coordinator`): one step, `survey`, with the probes, `record_finding` and `finish_analysis`. The step's exit is declared in the protocol's spec and covers what the next protocol reads: system and architecture, memory for models (RAM, VRAM, disk, path), graphics cards, local model runtimes and servers, official AI command-line tools (installed, whether a sign-in exists, never a token), the NAMES of API-key variables, and `analysis.finished`; every one at `tested` or better. The model chooses the probes and their order. `finish_analysis` is the terminal action and is offered once the rest of the exit's evidence is in (read from the spec by the loop). When the MODEL chose it and it succeeded, the GUARD closes the protocol (event `protocol_state` by `evidence`, tag GUARD, with `closed_by`) and the coordinator stores the analysis (`<state>/analyses/<node>.json`, each fact with its status and probe). `analysis.finished` is the guard's record that it accepted the declaration; `analysis.summary` is the model's own reading and a hypothesis. An analysis stored under a narrower spec does not count. Nothing is installed.
- **protocol-setup-options** (target kind `setup`): "which AI on this machine" is a separate run, started by the owner from the web screen. It does not start without a stored analysis (typed refusal `no_analysis`, no model call), and it reads that analysis through the model's `load_analysis` instead of probing again. The owner's questions are closed (ids and options come from the skill's `questions.json`); the options and their order are built by code with fixed filters; the model only explains each. `build_options` and `explain_option` close the step when an exit's evidence is in. The owner's pick is an OWNER event; nothing is installed.
- **protocol-lan** (target kind `lan`): the model lists the computers waiting to be paired; `pair_offer_start` refuses (conformance: `owner_pick_violations`) unless the OWNER picked that computer in this run.
- **Who started a run** is told, not assumed: the owner's press is an OWNER event in the run; a start or an answer from the operator (`by: claude-code` on the same endpoints, used only in a verification the owner authorized in chat) is logged as not the owner's, tagged CLAUDE-CODE, and the answer's fact says so in its evidence; it is never an OWNER event. CLAUDE-CODE is zero in a run the owner drove himself.
- **No code path advances a protocol for the model:** the loop moves a protocol only by the model's `advance`, by the guard closing a step on a tool the model chose (above), or by the experimental dial that the shipped profiles leave off (and that tags every move SCRIPT).

## 4. Discovery

4.1 The coordinator advertises itself on the LAN by mDNS (and a UDP broadcast fallback). A node looks for it, then dials it. Discovery only finds the coordinator; it authorizes nothing.

4.2 **Manual fallback:** the owner types the coordinator's IP and port and the pairing code on the node.

4.3 Discovery and connection stay inside the owner's own subnet (the existing scope guard applies) and are rate-limited.

4.4 Failures MUST end with a specific, typed, human-readable message and an event; none may end as a silent timeout. At minimum:
- `NET_PROFILE_PUBLIC`: "This Windows network is set to Public, so it may block the connection. Change it to Private in Windows network settings, or use another network. Nothing was changed for you."
- `NET_CLIENT_ISOLATION`: "The two computers are on the same network but cannot reach each other. The router may have client isolation (guest network) turned on. Use a network without it, or a phone hotspot."
- `COORDINATOR_FIREWALL_PENDING`: "The coordinator computer may be waiting for you to allow incoming connections for this program. Look for a firewall prompt on that computer and accept it, or open the port yourself."
- `COORDINATOR_NOT_FOUND`, `CODE_EXPIRED`, `CODE_REJECTED`, `PAIRING_REFUSED_BY_OWNER`.
Detection is read-only. The software MUST NEVER change an OS firewall rule, network profile or router setting by itself; it only tells the owner what to change.

## 5. Board

The board is held by the coordinator. Nodes do not talk to each other; they read and write the board through the channel. All entries are typed, signed envelopes.

5.1 **Node card:** node id, OS family, hardware summary, the owner's inference answer (7.4) and what was verified for it, installed harnesses, and **only capabilities that passed a test here** (with the evidence id and date). Untested claims MUST NOT appear.
5.2 **Task:** id, task type (any type that has an admission suite, 10.1), input, expected output, test (how success is checked by code), sensitivity (`public`, `internal`, `confidential`), created-by.
5.3 **Lease:** task id, node id, **deadline**, token (3.3), attempt number. A lease that expires without a result is released and the task is reassigned automatically; attempts are capped and the cap is a coordinator setting.
5.4 **Progress card:** lease id, step, short note; a progress card MAY extend the deadline within a coordinator-set maximum.
5.5 **Result:** lease id, output, **evidence** (distinct captures and channels, spec V1), and the result of the task's test, run by code.
5.6 **Published skill:** a skill (SKILL.md folder) that passed its tests and the verifier, with its hash and the sensitivity of the data it was built from.

## 6. Sensitivity

6.1 Levels: `public`, `internal`, `confidential`. Every task, board entry, fact and file derived from a task carries a level. A derived item takes the **highest** level of its inputs; only the owner can lower a level.

6.2 Enforcement is in code, at three points: when the router picks a combination (section 10), at the node's model and network calls (through the proxy in 8.2), and at the coordinator when it accepts or serves board entries. It MUST NOT depend on a prompt.

6.3 `confidential`: never sent to a cloud model or to a commercial harness (6.6); never routed to a machine that is not one of the owner's paired nodes; no outbound agent-to-agent traffic; web research none, or fixed owner-approved domains only; the **planner is always a local model**. A node with no suitable local model cannot take the task.
6.4 `internal`: cloud models and egress only if the owner's policy allows it, and only through filters (redaction of identifiers, size cap, logged).
6.5 `public`: no restriction beyond the owner's policy and the hard limits.
6.6 A **commercial harness** counts as **cloud** for sensitivity, whatever inference mode it uses. A confidential task is never routed to one (conformance R1).
6.7 A combination that is only **provisional** (rests on public evidence and hardware fit, not measured here) never receives a confidential task (conformance R3; RECOMMENDATION_SPEC 7.3).

## 7. Platform

7.1 Python on the coordinator and on every node. Installation uses a small installer built on **uv**, which provides its own Python, on Windows, macOS and Linux. The installer is run once by the owner, shows what it will do before it does it, needs no inbound firewall rule, and has an uninstall.

7.2 A **per-OS layer** (`darwin`, `windows`, `linux`) behind one interface carries the probes. OS-specific knowledge (PowerShell, brew, apt, winget, service managers) lives in **SKILL.md skills per OS family**, not in Python branches (spec S2).

7.3 Support matrix. Cells say planned (intended), untested (code exists but never ran there) or verified, and every "verified" carries the label of what was run: **REAL** (a real device), **SIMULATED-LOCAL** (two processes on one machine, over loopback) or **LINUX-CONTAINER** (a Linux container on this Mac). A loopback or container run is never called a multi-machine run.

| Platform | Arch | Discovery | Node agent | Coordinator | Installer | Sandbox |
|---|---|---|---|---|---|---|
| Windows 10/11 | x86_64 | planned | untested (code is OS-independent; the Windows ACL step has never run) | untested | planned | open (research) |
| Windows 10/11 | arm64 | planned | untested (and `cryptography` has no native wheel newer than 46.0.3) | untested | planned | open (research) |
| macOS | Apple Silicon | **verified (REAL)** (this Mac, Ring 0, LAN) | **verified (SIMULATED-LOCAL)**: pair, heartbeats, 0 listening sockets | **verified (SIMULATED-LOCAL)**: pair, heartbeats, exactly 1 listening socket | planned | planned (Seatbelt) |
| macOS | Intel | untested | untested (`cryptography` 48.x is the last with a wheel) | untested | planned | planned (Seatbelt) |
| Linux Debian/Ubuntu | x86_64 | untested (containers only) | untested | untested | planned | planned (bubblewrap or Landlock+seccomp; Docker if installed) |
| Linux Debian/Ubuntu | arm64 | untested (containers only) | untested on Debian/Ubuntu; **verified (LINUX-CONTAINER)** on Alpine Linux, Python 3.12: pair, heartbeats, 0 listening sockets | untested | planned | planned |

**Probe coverage (the six read-only probes of 3.8 and the per-OS layer `envrec_os`).** Same labels. Honest state on 2026-10-30:

| Operating system | x86_64 | arm64 |
|---|---|---|
| Windows 10/11 | UNTESTED (fixtures are hand-written; real captures need a real Windows machine) | UNTESTED |
| macOS | UNTESTED | **REAL** (this Mac: the six probes ran locally, and through a node as SIMULATED-LOCAL) |
| Linux Debian/Ubuntu | UNTESTED | **LINUX-CONTAINER** (debian:bookworm-slim on this Mac: all six probes run; no GPU, runtimes or CLIs, with named limits) |
| Linux Fedora | UNTESTED | UNTESTED |

A LINUX-CONTAINER run says nothing about a real GPU, a real package manager database or a real desktop.

Everything not marked verified is untested or planned today. Only macOS discovery on one Apple Silicon machine has ever run on a real device (REAL); the network layer has run only as two processes on that Mac and as a node inside a container on it.

7.4 **Inference on a node starts with the owner.** During node setup the owner is asked which AI to use on that machine: open-source local; their own subscription through the vendor's official CLI (which one); their own API key (which provider); or a combination. The answer is stored in the node card and the owner can change it at any time. Nothing is assumed from what happens to be installed.

7.5 **The node verifies only what the owner selected**, and never reads secrets:
- local: the runtime is present and the model fits in memory (and the loopback rule of 1.3 holds);
- subscription: the official CLI is installed and logged in; the node MUST NOT read, copy or print any token;
- API: the environment variable of the stated name exists; the node MUST NOT read, print or store its value.
Anything the owner did not select is not probed.

7.6 Commercial CLIs are used only under 9.4.

## 8. Sandbox

8.1 One sandbox interface, with backends: **Seatbelt** (macOS); **bubblewrap** or **Landlock+seccomp** (Linux); **Docker** where installed (existing backend, today the only one); **Windows: open, to be researched** (no claim is made).

8.2 Rules for every backend: writes only inside the workspace; no network except through a local proxy that enforces a **domain allowlist chosen by the task's sensitivity** (6.3, 6.4); no device access; resource limits; fails closed.

8.3 A node with **no available backend MUST NOT run model-written code**. It MAY use only closed recipes, the action menu and per-command owner approval.

8.4 Third-party harnesses run **whole inside the sandbox**, so everything they launch inherits the restrictions; their config and cache directories point inside the workspace.

8.5 Model-written code is run only through the sandbox gateway, which refuses before starting anything if no backend is ready (conformance N3).

## 9. Workspace self-setup as harness setup

9.1 The user states what they want to do on a machine and, first, which AI they want to use there (7.4). The steps below are enforced by code; the model proposes nothing in steps 9.3 to 9.6.

9.2 **Every harness enters through a manifest** of facts: the task types it claims, OS support, accepted models / providers / inference modes, whether it needs a sandbox, Docker or a GUI, its license, and its upstream (repository and pinned commit for open source, vendor page for commercial). **Every manifest field is labeled** `verified by code` or `declared by the project` (with the exact quote and its source). Any kind of harness may be routed: coding agents (open source or commercial CLIs), browser agents, data agents, general tool agents, our own setup harness, or a direct model call with no harness. Field list, labels and the catalog protocol: RECOMMENDATION_SPEC sections 2 and 3.

9.3 **Hard filters (code):** the OS is supported by the manifest; the model fits in the machine's memory; for anything **we install**, the license is **MIT or Apache-2.0, confirmed by code in the upstream LICENSE file at the pinned commit** (not asserted from memory or a listing); the harness is compatible with the task's sensitivity (6); a sandbox backend is available here if it needs one; Docker is present if it needs Docker; a GUI is present if it needs one; and the inference mode is one the owner selected.

9.4 **Commercial harnesses.** They are **never bundled, copied or installed by us.** They are used only if the owner selected them, they are already installed and logged in with the owner's own account, and the node verified that under 7.5. For sensitivity they count as **cloud** (6.6). Each vendor's terms get a manifest field `automated use`: `verified` or `to review`. `to review` means: shown to the owner as an option, **never auto-routed**.

9.5 **Options.** Candidates are generated by code and shown at two speeds (provisional in seconds, measured in the background). The options shown are the **Pareto front** over pass rate, cost and time, usually 2 to 3, with the evidence label of each and **no "best" label**. The model gives no opinions. The owner chooses. Rules: RECOMMENDATION_SPEC sections 5 to 10.

9.6 Then: install inside the sandbox with approval (undo recorded first), run the **admission exam** for that machine + model + harness combination (threshold 8 of 10 by default, configurable by the owner, always displayed), and write the real result to the node card.

9.7 **Catalog.** There is no fixed list of harnesses in this contract. The catalog is built by the written protocol in RECOMMENDATION_SPEC section 2. **Docker-dependent harnesses** (for example OpenHands and SWE-agent) are allowed only on nodes that have Docker, and are not part of the demo.

9.8 **Unknown harnesses** climb the existing ladder: research official documentation, propose a skill, test it in the sandbox, clean-context verifier, the owner approves the exact code once by its hash, and later runs reuse it with zero model calls.

9.9 **Sending or migrating a setup is sending that recipe** (with its hash); the receiver verifies the hash and accepts or refuses. Pinned commit and license for each harness are recorded in `THIRD_PARTY.md`.

## 10. Router (design only)

10.1 **The router chooses a combination:** harness x model x inference mode (local / API / subscription CLI) x machine, for a task type. It is **harness-agnostic**: any harness kind enters through a manifest (9.2). The set of **task types** is the union of the task types declared in manifests and the task types defined by task packs; **each task type MUST have an admission suite before the router can use it** (RECOMMENDATION_SPEC 8 and 9).

10.2 **Steps, in order:** (1) filter by the task's **sensitivity**; (2) filter by **admission** (only combinations approved for this task type on that machine; a provisional combination only where 6.7 allows); (3) pick by the **written selection rule** of 10.3; (4) on failure, **escalate** under 10.4.

10.3 **Selection rule, in this order:** (a) **cost class**: local < API < subscription; (b) **admission pass rate**, where differences inside a fixed **tie margin** count as a tie (the margin value is **OPEN**); (c) **measured time**; (d) **fewer active leases**.

10.4 **Escalation:** at most **2 steps** up, and only where the sensitivity allows; then stop and notify the owner on the board (conformance R2).

10.5 **Failure** means the task's code grader (or its result verification) fails. **Cost** is the cost class first, then measured cost and time. The **task type is chosen by whoever creates the task**; the local planner may suggest one; **no cloud classifier ever sees a confidential task.** (These three were open questions in revision 1 and are closed here.)

10.6 **The model never chooses the route and never ranks options.** A router decision is an event of type `route_decision`, tagged `ROUTER`, listing the candidates, the rule that applied and the choice, so it can be recomputed from stored admission results alone.

10.7 Confidential tasks go only to combinations **measured here**, never to cloud, to commercial harnesses, or to machines that are not the owner's paired nodes (R1, R3).

10.8 **OPEN:** the tie margin value; the shortlist size and per-mode representation numbers; how measured time and cost are normalized across machines; the display cap on options (RECOMMENDATION_SPEC 13).

10.9 **Optional experiment, out of scope for now:** a hosted decision model (for example Jev) that suggests a task type. Only after everything else works. It is cloud, so it never sees a confidential task.

## 11. Rule changes this contract makes

- The model still chooses its own action each turn from the closed menu. The router (code) may choose the harness/model/machine combination, but only among combinations that passed admission and that the sensitivity allows, by a written rule.
- Authorization is `targets.yaml` **or** a pairing confirmed by the owner on the coordinator.
- Each node runs its own inference locally and reaches the coordinator only through the node-initiated signed channel; there is no controller-opened tunnel to a node.
- The router is harness-agnostic (10.1): the fixed three task types and the fixed Aider / smolagents / direct-call catalog of revision 1 are removed; harnesses enter through manifests and the catalog protocol.
- The loopback exception of revision 1 (1.3) is replaced by: our node agent opens zero sockets with no exception; only a third-party inference server bound to loopback is allowed.
- The fixed `max_workers: 3` is replaced by the lease model (one lease per task, deadlines, reassignment); any safety cap is a coordinator setting.

## 12. To build in Phase 1 (names only; none built)

- **First item, door gap:** persist the door events that happen after the last tool call (link expiry, shutdown), with a test for "the link expires with no later tool call". The door route is retired, but its events and the way they are queued are reused by the node-connect protocol.

- `protocol-pairing`: code, key exchange, owner confirmation on the coordinator, identity keys. **Built as `protocol-pair`** (Phase 1 step 1).
- `protocol-node-connect`: find the coordinator, mutual TLS, heartbeat, reconnect, typed failures (4.4). **Built without discovery and typed failures (4.4 is still to do)**: the node dials a given address, heartbeats and reconnects with backoff.
- `protocol-board-lease`: take, progress, extend, release, expire and reassign a lease.
- `protocol-admission-exam`: test a machine+model+harness combination per task type, produce the node-card entry.
- `protocol-harness-setup`: filters, options, owner choice, install in sandbox, exam (section 9).
- `protocol-inference-setup`: ask the owner which AI to use on the node, verify only that (7.4, 7.5), store the answer in the node card.
- `protocol-harness-manifest`: read, label and verify manifest fields; clean-context quote verification.
- `protocol-catalog`: run the written catalog protocol, publish the funnel, snapshot and control-set result.
- `protocol-route`: the router (section 10), once its OPEN items (10.8) have values.
- Task packs and admission suites per task type (RECOMMENDATION_SPEC 8 and 9).
- **First item of the next step:** Windows discovery skills for `machine_analysis` (installed programs, GPU, local model runtimes such as Ollama, AI command-line tools installed), so a Windows node's analysis is as complete as on macOS and Linux: knowledge in skills and the per-OS layer, read-only, no new dependency without its license and wheel check.
- (Done in Phase 1 step 1b) The owner's web screen of 1.7: machines and pairing, loopback only, session by one-time link, Host/Origin/CSRF checks; `mas coordinator start --web`. Not built: recommendation screens, chat, board, router.
- (Done in Phase 1 step 1) `config/models.yaml` local endpoints are `127.0.0.1` and N5 scans that file.
- `skill-os-windows`, `skill-os-macos`, `skill-os-linux`: per-OS-family know-how (probes, install, services, paths).
- `skill-sandbox-setup`: check and report which sandbox backend is present on this node.

## 13. Old plan, mapped

- **Mission 1** (set up a workspace on the second computer, with approvals, verify, undo) **merges** into node setup over the network (section 9); only its transport changes.
- **Stage 2** (the model builds a workspace/adapter from documentation) **becomes the ladder** (9.8).
- **Driver creation** is dropped as a separate stage; the ladder covers it.
- **Mission 2** (earbuds/Spotify) is dropped from the plan. Its code stays in the repository, is not run, and is not deleted.

## 14. Conformance

`docs/HARNESS_SPEC.md` section NETWORK (N) and sections ROUTER (R) and ADMISSION (A) list the checks. Today: **N1** node code opens no listening socket; **N2** coordinator code opens exactly one LAN-facing listening socket and at most one more, bound to loopback (the owner's web screen, 1.7); a second socket that is not a loopback literal fails (the runtime side is N4); **N3** model-written code runs only through a gateway that refuses without a sandbox backend; **N5** default and configured inference endpoints used by node code are loopback literals (static; **N5-runtime** is a skipped placeholder). **R1** confidential tasks are never routed to cloud, commercial or third-party machines; **R2** escalation never exceeds 2 steps; **R3** provisional combinations never receive confidential tasks; **A1** only code-graded tasks count toward the admission threshold: all four are skipped placeholders with a stated reason until the router and the admission exam exist. Each check that can run today has a fixture that violates it and is shown to fail.
