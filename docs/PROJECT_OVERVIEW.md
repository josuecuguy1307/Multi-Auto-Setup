# Project overview: Multi Auto Setup (MAS)

MAS discovers different computers on the same network, understands their capabilities, prepares machine-specific AI environments and coordinates them securely as one intelligent system. One trusted computer can discover another, assess its capabilities and prepare its environment under security rules and owner approval: controlled self-provisioning, never self-replication.

**Discover computers. Understand their capabilities. Prepare and equip their AI environments. Bring together models, agents, harnesses and specialized tools. Coordinate different machines securely as one system.**

## 1. Engineering achievements

| Area | What MAS does | Where it lives |
|---|---|---|
| Machine discovery and inspection | Reads operating system, CPU, memory, GPUs, storage, installed software, AI runtimes, models and available tools as typed, evidence-tagged facts (status, provider, raw capture by hash) | Ring 0 probes and discovery skills in `skills/` (`os-info`, `hw-*`, `app-*`, `net-*`, `usb-devices`, `ble-*`, `lan-*`); six read-only machine probes through the per-OS layer (`envrec_os`) |
| Recognition and secure pairing | A Mac coordinator and a Windows Lenovo were discovered, recognised and securely paired: 6-digit code over an authenticated key exchange (PAKE), Ed25519 identity keys pinned on both sides, mutual TLS 1.3, signed envelopes | `protocol-pair`, `protocol-node-connect`, `src/envrec_net/`, `src/envrec_coordinator/`, `src/envrec_node/`; contract in `docs/NETWORK_CONTRACT.md` |
| Machine-specific preparation | Analyses each machine, builds options that fit its measured memory, disk and graphics, and prepares its environment with controlled, pinned setup recipes | `protocol-analyze`, `protocol-setup-options`, `protocol-setup`, `setup-recipes`, `model-catalog-rules`; the Setup Builder workflow (`envrec_coordinator/setup_plan.py`) |
| Installation and configuration | Installs and configures AI models, runtimes, agents, harnesses and tools from pinned catalog data (addresses, sizes, SHA-256), each write step approved and undoable | Setup recipes as data; node executors in `envrec_node`; tool installation from GitHub release assets (`mas_install_tool`) |
| Coordination | Coordinates different computers and AI environments over the same network through one coordinator, a typed board with leases, and a deterministic Router | Board and Router in `envrec_coordinator` (`docs/BOARD_API.md`, `docs/ROUTER_API.md`) |
| Security architecture | Encrypted communication (mutual TLS, signed hash-chained event log), pairing, enforced protocols, sensitivity levels in code (`public`, `internal`, `confidential`), permissions and capability tokens, controlled setup recipes, fresh human approval for every write, and verification from the world by code | Guards in code; conformance suite in `conformance/` (`pytest conformance`) encodes `docs/HARNESS_SPEC.md` and the network contract |
| Repeatable workflows | Skills capture know-how once and reuse it; a second encounter with the same signature runs without a model call | SKILL.md library in `skills/` with per-device verification state |
| Linux testing | The node, coordinator, installers and probes were exercised on Linux as part of the engineering validation | Recorded with labels in `PROGRESS.md` |

## 2. Work performed with MAS

MAS brings together the appropriate computers, models, harnesses and tools for different kinds of work. Concrete workflows exercised through MAS and its chat clients include:

- **Coding and software development** — tasks routed to coding environments on the machine that fits them (Section 4).
- **Software installation and environment setup** — analysis, options, an approved plan, installation, and a verified minimal call.
- **CSV processing and data analysis** — the catalog task types `data-cleaning` and `numeric-analysis`, served by local and provider environments.
- **Financial reporting and report generation** — free-form requests that no catalog type names go through the Recommendation Engine (`protocol-recommend`), which proposes a suitable environment and asks the owner one question before any task is created.
- **Interactions with different AI agents and clients** — the owner works through Muse Code, Claude Desktop, Claude Code and Open WebUI (Section 5) while MAS supplies the tools, guards and coordination behind them.

## 3. Harnesses by kind of work

A harness is the execution and tool-access environment an agent or model works inside. MAS is harness-agnostic: any kind enters the catalog through a labelled manifest (verified-by-code vs declared-by-the-project fields).

Actual MAS catalog environments (`skills/router-environments/references/environments.json`):

| Environment | Kind | Serves | Evidence state |
|---|---|---|---|
| `llamacpp-local` | local model (llama.cpp loopback server) | text, code, summarize, extraction, data-cleaning, numeric-analysis | tested |
| `claude-code` | provider CLI | coding domain (same task types) | tested |
| `codex` | provider CLI | coding domain | verified |
| `muse-code` | provider CLI | coding domain | hypothesis |
| `opencode` | own harness | coding domain | verified |
| `pi` | own harness | coding domain | verified |
| `meta-model-api` | provider API | text, code, summarize, extraction, data-cleaning, numeric-analysis; coding, image-generation, speech-recognition domains | tested |
| `goose` | own harness | general task types | hypothesis |
| `legal-contract-review-skill` | skill pack | legal domain | hypothesis |

Task-oriented external references (background reading, **not** MAS integrations; MAS does not claim to have installed them):

- Coding: OpenHands (https://github.com/OpenHands/OpenHands) and the OpenAI Codex harness (https://developers.openai.com/blog/codex-as-a-platform) illustrate repository-oriented coding agents with tools, sessions, sandboxing and approvals.
- CSV/data analysis: Microsoft Agent Framework's `harness_data_processing.py` example (https://github.com/microsoft/agent-framework) illustrates an agent harness for file and CSV processing.
- Financial reporting: the Personal Finance Claw example (https://devblogs.microsoft.com/agent-framework/agent-harness-working-with-your-data-safely/) illustrates reading financial CSV data with tools and generating reports.
- Installation/environment preparation: the OpenHands Agent Server / software-agent-sdk (https://github.com/OpenHands/software-agent-sdk) and the Codex execution-environment architecture (https://developers.openai.com/api/docs/guides/agents-api/architecture) illustrate agent execution in configured local or remote environments.

## 4. Recommendation Engine and Router

The two are distinct, and neither is the model's opinion:

- **Recommendation Engine** (`protocol-recommend`, `envrec_coordinator/recommend.py`): explores suitable tools, models, harnesses and environments for a requested type of work. It identifies a default and a runner-up, missing components, and setup possibilities (for example `mas_install_tool` when the harness can install an entry after an approved plan, or a typed `no_installer` / `unverified_entry` state when it cannot). For an unknown request it researches official sources, records leads as hypotheses, and asks the owner one question bound to a plan hash; only then is a task created.
- **Router** (`envrec_coordinator/router/`, `docs/ROUTER_API.md`): evaluates eligible combinations — harness x model x access x machine — and selects by a written deterministic rule: sensitivity filter first, then admission, then cost class (local < API < subscription), admission pass rate within a tie margin, measured time, and fewer active leases. Every decision is a `route_decision` event tagged ROUTER with its candidates and reasoning. The model never chooses the route.

What the options depend on: machine capabilities (measured OS, memory, GPU), installed programs, model availability and fit, privacy requirements (a confidential task never goes to a cloud model, a commercial harness, or an unpaired machine), permissions and owner profile choices, admission information (measured here vs provisional public evidence), and cost class.

Concrete mappings from the catalog (options, not invented history):

- **Coding task** → `codex`, `opencode`, `pi`, `claude-code` or `muse-code` where installed and admitted, else `llamacpp-local`.
- **Software setup task** → the Setup Builder chain: `protocol-analyze` → `protocol-setup-options` → `protocol-setup` with `setup-recipes` data and `llamacpp-local` installed by recipe.
- **CSV / data-analysis task** → task types `data-cleaning` / `numeric-analysis` on `llamacpp-local`, `meta-model-api` or a provider CLI, subject to sensitivity.
- **Financial-reporting task** → a free-form request enters `protocol-recommend`; the recommender checks the catalog and its domain evidence, proposes an environment and its tools, and the owner approves before the task exists.

## 5. AI client integrations

The owner's chats reach the same harness core through adapters that only translate (`docs/HARNESS_APP_STANDARD.md`); a write is approved only by the owner's human gesture, never by a chat model's tool call.

- **Muse Code** — connected as a chat host (`mas connect`); interacts with MAS tools over MCP (stdio and streamable HTTP on `/harness/v1/mcp`).
- **Claude Desktop** — MCP adapter over stdio with rendered cards; approvals through the host's own dialog.
- **Claude Code** — terminal-agent host type, connected the same way, with live activity of the machines visible in the chat.
- **Open WebUI** — through its Native Pipe integration (`envrec_harness/openwebui_pipe.py`), receiving the same intents and cards.

Through these interfaces the owner can recognise machines (the network and machine cards), start analyses, answer the harness's questions, and approve setup steps — while pairing codes and secrets never pass through a chat model.

## 6. Tools used to build MAS (development only)

Kept strictly separate from what MAS operates:

- **Claude Code** (Claude Sonnet) — one of the two coding agents that built MAS, used throughout its construction.
- **Muse Code** (Meta Muse Wonder) — the other coding agent that built MAS, including the local-model harness work, the Open WebUI integration, and this documentation and release work.
- **Claude Design** — used for design work.
- **Smaller local Qwen models** (for example `qwen3.5:9b`) — used during development, experimentation and MAS workflows as the local actor that chooses actions inside the harness.
- **Orchestration and verification** — direction, decisions, review and verification with the test and conformance suites by the owner, Josué.

## 7. Models MAS works with

- **Meta Muse Glimmer / Muse Spark** — the profile names in `config/models.yaml` (`muse-glimmer`, `muse-spark`; OpenAI-compatible adapters, endpoints and model ids supplied per deployment). Roles: the small and strong harness models in the submission configuration.
- **Smaller local Qwen models** — the local actor in experiments and runs (Ring 0 recognition scored 17/17 correct with 0% false-verified on this Mac; the ladder and network workflows were driven by the local model choosing from the closed menu).
- Development models (Section 6) are not MAS integrations; the lists never mix.

## 8. Skills, protocols and workflow architecture

- **Harnesses** provide the execution and tool-access environment for agents and models (Section 3).
- **Skills** provide reusable instructions, knowledge and workflow guidance (`SKILL.md` folders; the menu shows name and description only, the body loads when chosen).
- **Protocols** define structured processes and their required transitions, enforced by the loop as state machines (for example `protocol-pair`, `protocol-task`, `protocol-setup`, `protocol-undo`, `protocol-recommend`, `protocol-analyze`).
- **Guards** enforce the rules and evidence requirements in code: authorisation, sensitivity, approvals with undo recorded first, sandbox that fails closed, privacy gate at the fact boundary.
- **Setup recipes** (`setup-recipes`) define controlled installation and configuration operations as data: pinned addresses and hashes, the paths each step writes, and its undo.
- **Setup Builder** organises the environment-preparation workflow: analyse the machine, build fitting options, take the owner's pick, build the plan from a recipe, run each approved step, verify with a minimal call.
- Supporting skills include `router-environments` (the catalog and policy as data), `harness-tools` (what the chats may call), `os-confirm` (the operating system's own approval and notice dialogs), `harness-app-chat`, `owner-profile`, `secret-entry` and `mas-voice`.

Connected to the work: coding runs inside a coding harness chosen by the Router; software setup runs through the Setup Builder and its recipes; CSV and financial work enters through the catalog task types or the Recommendation Engine; computer provisioning is the pairing + analysis + setup chain across two real machines.

## 9. External architectural inspirations

Ideas adapted, no code copied (attribution and roles in `THIRD_PARTY.md`):

- **REA** (`rea-agents`) — inspiration for evidence-based discovery: typed facts with confidence, unknowns recorded honestly, and verification independent of the claim. MAS's fact statuses (`hypothesis`, `tested`, `verified`, `contradicted`, `unknown`) and its evidence rule come from this line of thinking; the REA tool itself was run via its CLI and mapped (verdict: lossy), not transplanted.
- **BusPirate MCP** — inspiration for tiered tool permissions and controlled access. MAS's tool tiers (observe vs read-tier with approval, writes only behind a fresh owner approval) adapt that idea in its own policy code; the tier idea was rewritten, not copied.

## 10. Releases

- `node-preview-10` — MCP over streamable HTTP for chat models; public README lists only verified features. Published without the interactive terminal.
- `node-preview-11` — the interactive terminal for `mas` (Home, Computers, Help, Questions, Progress, plain-language errors, `mas preview`) on node-preview-10, styled from the single token source. Archive SHA-256 `33bca7a2a122ed4a8d0453360bda2b6044b50421435a73291d8ea3f47d5b1464`, verified after download from the public release.

Development history, run-by-run evidence and honest labels (REAL, SIMULATED-LOCAL, LINUX-CONTAINER, UNTESTED) are in `PROGRESS.md`. The rules are in `CLAUDE.md`, `docs/HARNESS_SPEC.md` and `docs/NETWORK_CONTRACT.md`.
