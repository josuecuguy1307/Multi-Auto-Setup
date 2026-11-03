# Harness App standard

The Harness App is how an owner works with the harness (Multi Auto Setup) from a chat they already use. We do not build a chat and we do not modify one: the harness lives behind each chat's own extension points, and a thin adapter translates. This document is for people who want to connect the Harness App to their own chat. It is the contract between the core, the adapters and the cards. Every host and every part carries a status label; nothing is claimed that was not run.

Status labels used throughout: **REAL** (run on real software by a person at the keyboard), **SIMULATED-LOCAL** (real coordinator and real node processes on loopback, with the chat or the model played by a test), **LINUX-CONTAINER**, **UNTESTED**.

## Principles

1. **One core, adapters that translate.** The core (`envrec_coordinator/harness.py`) owns runs, intents, who may decide what, and the approval rules. An adapter (`envrec_harness/mcp_server.py`, `envrec_harness/openwebui_pipe.py`) maps a chat's calls onto the core's routes and reports what the core said. Conformance HA1 fails an adapter that branches on sensitivity, names an owner, or runs a program.
2. **The harness emits intents, not screens.** What the harness says is an `intent`. Each host renders it with its own native elements and, where it helps, with our cards.
3. **Native and cards are both first-class, and text always works.** A chat's own elements carry what the chat does well (questions with options, confirmations, progress, text). Our HTML cards carry what it lacks (the live network map, the machine card with evidence state, options compared with symbols, the setup plan, a run with actors). The text form of every intent is always present and stands on its own, so a chat that shows no card still works.
4. **A chat model is never the owner.** A tool call from a model never approves anything that writes on its own. A write is approved by a human gesture (see Approvals); the one exception is the plan of a setup or an undo, which the owner's data (`approval-policy.json`) leaves to ONE chat question, relayed by the model, recorded as the weaker `chat_choice` gesture and never as a system dialog.
5. **Sensitivity is enforced in code.** A cloud chat receives summaries only.
6. **The model decides, code guards.** Inside a run the harness's own model chooses one validated action per turn from a closed menu; code validates, refuses, executes and records. Every recorded event carries its actor: MODEL, GUARD, SCRIPT, OWNER or CLAUDE-CODE.
7. **English, in one place per package.** Every owner-facing word (chat text, dialogs, cards, panel, errors) is English and lives in a strings module: `envrec_harness/strings.py`, `envrec_coordinator/strings.py`, `web_static/strings.en.json`. The Open WebUI Pipe is a single pasted file, so it keeps its own `S` block at the top.

## Hosts and their status

| host type | how it connects | what it shows natively | confirms a write with | status |
|---|---|---|---|---|
| **Open WebUI** (a local chat) | a Function (Pipe) pasted in Admin Panel; no change to Open WebUI | select dialogs, status line, embedded cards, text | the chat's own confirmation dialog (`host_dialog`) | **REAL** on Open WebUI 0.12.5: menu, network map card, analysis (real local model), options, pick, setup plan text, one step approved in the chat's dialog, undo in a new run. The node, the runtime download and the setup model were simulated (loopback origin, fake runtime, scripted model) |
| **Claude Desktop** (a provider chat) | a local stdio MCP server in `claude_desktop_config.json` | tool results as text | the operating system's dialog (its handshake declares **no** elicitation) | **REAL**: handshake, tool calls, the OS dialog. **Cards are not shown (known gap, below).** The Touch ID prompt after the dialog is UNTESTED in the owner-visible sense (the log recorded that it was requested) |
| **Terminal agents** (Claude Code, Codex CLI, Gemini CLI, opencode, ...) | the same stdio MCP server, on channel `terminal` (provider model) or `terminal-local` (model on this computer) | tool results as text | an elicitation form when the agent's handshake declares one, otherwise the operating system's dialog | **SIMULATED-LOCAL** (a scripted host over the real adapter). **UNTESTED with a real terminal agent** |
| **Owner panel** | the coordinator's own loopback screen | approvals, the live activity list, the map | a click, with the plan hash in the request (`panel_click`) | **SIMULATED-LOCAL** (unit tests of its approvals and activity list); not driven in a browser in these verifications |
| Another chat | a new adapter (see "Adapt another chat") | whatever it has | per its channel | UNTESTED |

The OS confirmation itself: the macOS dialog is **REAL** here; the Windows message box, Windows Hello and the Linux `zenity`/`kdialog` dialogs are **UNTESTED**.

Recommended hosts. For confidential work, a local chat (Open WebUI with a local model) is the only kind that receives everything its sensitivity allows; a cloud chat gets summaries. For the richest view today, Open WebUI (cards render inline). Claude Desktop and terminal agents give the full flow as text with the OS dialog for approvals.

## The `intent`

```json
{"id": "...", "seq": 7, "ts": 0, "kind": "ask_choice|confirm|progress|show_data|text", "run_id": "...",
 "actor": "MODEL|GUARD|SCRIPT|OWNER|CLAUDE-CODE", "sensitivity": "public|internal|confidential",
 "payload": {}, "text_fallback": "..."}
```

| kind | payload | notes |
|---|---|---|
| `progress` | `type`, `important` | one line per thing that happened, naming the machine, what happened and who did it; merged and rate-limited (see "Live activity") |
| `ask_choice` | `question`, `text`, `options[{id,label,detail}]`, `writes` | `writes` must be stated. The text fallback lists every option as a numbered line (`1. label [id: x]`), so a text-only chat can answer by number or by id |
| `confirm` | `writes`, and for a write: `summary`, `command`, `undo`, `plan_hash`, `step`, `of`, `action` (`run`\|`undo`), `targets`, `bytes` | a write always shows its exact command, how to undo it, and is bound to a `sha256:` plan hash |
| `show_data` | `data_kind` = `network` \| `machine` \| `options` \| `plan` \| `run`, `data` | rendered by a card in hosts that can |
| `text` | | |

`validate()` refuses an intent without a text fallback, with an unknown kind, actor or sensitivity, a choice that does not say whether it writes, a write without command, undo or plan hash, and **a write whose command, undo or summary holds angle brackets** (a chat may strip them as HTML tags; see "The text a human approves").

## The core API

Served by the owner's loopback screen under `/harness/v1/` (no new socket: conformance N2/N4 still hold). Each channel has its own token file in `<state>/coordinator/harness/<channel>.token` (owner-only), plus `endpoint.json`. The channel is the token's, never a header the client writes. A request with an `Origin` or a cookie (a web page) is refused; the Host check applies. Channels: `claude-desktop`, `openwebui`, `terminal`, `terminal-local`, `cli` (the owner's own terminal for `mas connect`: no model in the middle; the owner's typed `yes` is the gesture) (and `panel`, which has no token: it is the owner's screen).

| route | what it does |
|---|---|
| `GET/POST capabilities` | the channel's rules and the confirmations available to it; the adapter declares what the live handshake said (`elicitation`, `mcp_apps`, `event_call`, ...) |
| `POST view` | how the host displays our view (display mode, size), kept as evidence |
| `GET network` | the map as an intent (nodes, status, activity, owner-set kind) |
| `POST runs` `{goal, node}` | start a run. `goal` is `analyze`, `options`, `prepare` (set up the option the owner picked), `undo` (undo a finished setup, in a new run), `connect_host` or `disconnect_host` (connect or disconnect a chat on this computer: `node` carries the chat's name, or nothing to be asked). A node may be named by name or id. `sensitivity` is accepted for analyze and options |
| `POST pairing` `{machine?}` | pair a new computer. Without `machine`: who waits to join, the code in use, the computer that typed the code and needs the owner's approval, or the last outcome. With `machine` (a waiting computer's name or id): a 6-digit code for THAT computer, shown to the owner. Refusals: `not_waiting`, `ambiguous_machine`, `pairing_busy`. A chat's request is logged as `pairing_offer_requested_by_chat` (not an OWNER event) |
| `POST unpair` `{machine}` | ask the owner to unpair a paired computer: returns a confirmation and removes nothing. Refusal: `unknown_node` |
| `GET runs/<id>/intents?since=&wait=` | the typed stream of one run, long-polling up to 20 s |
| `GET activity?since=` | the live lines of **every** run and of the tasks no run owns, as this channel may see them; returns `seq` to continue from |
| `POST decisions` `{intent_id, option}` | answer a closed question **that writes nothing** |
| `POST confirmations` `{intent_id, mechanism}` | start a confirmation of a write; returns a ticket, the plan hash and the prompt text |
| `POST confirmations/<ticket>/complete` `{plan_hash, action}` | the host's answer, for host-answered mechanisms |
| `GET confirmations/<ticket>` | state of a ticket (the OS dialog resolves on its own) |
| `GET card?intent_id=&host=&fragment=` | the card of a `show_data` intent, wrapped for `panel`, `openwebui`, `mcp` or `plain` |

Owner-panel only (needs the panel's session): `/api/run/answer`, `/api/confirm/decide`, `/api/install/start`, `/api/uninstall/start`, `/api/connect/start`, `/api/node/kind`, `/card/network`, `/card/intent`. A chat's token opens only `/harness/v1`.

## The flow, from the owner's side

1. **See the network** (`network`): the map and, as text, each machine's role, system, state, what it is doing and who did it, with its resources and installed AI and the evidence state of each fact. Paths are left out of the text.
2. **Analyze** a machine (`runs` goal `analyze`): a model chooses read-only probes; the machine card follows. **Which AI?** is a closed question; **options** come back as an unordered set, each with its model, quantization, file size, memory needed, memory usable and whether it fits. Nobody recommends one: the owner **picks**.
3. **Prepare** (`runs` goal `prepare`): the setup run builds a plan from a closed recipe and the machine's measured facts. **The whole plan reaches the chat as text before the first approval**: every step with its exact command, what it writes, its size and its undo, the memory numbers, the plan hash and who may approve. Each step is then its own approval; progress lines follow; the last step is a minimal call to the local server, whose latency is recorded as a tested fact.
4. **Undo** (`runs` goal `undo`, later and in a new run): the coordinator keeps what a setup left on each machine (the plan that ran and where each step stands). The undo plan is told first as text: only the steps that are done, newest first, each with the undo that plan itself wrote. Each undo step is approved like any write.
5. **Follow the machines** (`activity`): see "Live activity".

## Approvals

A write needs a **confirmation**: a single-use ticket (150 s) bound to the exact plan hash, for one channel, answered by one of:

| mechanism | who answers | when |
|---|---|---|
| `host_elicitation` | the host's own UI (MCP elicitation, form mode); the adapter sends the core's text and the plan hash | only if the live handshake declared `elicitation` |
| `host_dialog` | the chat's own confirmation dialog (Open WebUI `__event_call__`); no other model sits between the owner and our Pipe | only if the chat declared `event_call` |
| `cli_typed` | the owner's own terminal (`mas connect`): the owner types `yes` at their keyboard; no model sits between | only if the terminal declared `event_call` |
| `os_dialog` | the operating system's native dialog, raised by the coordinator on top of the chat. macOS adds LocalAuthentication (Touch ID or the account password) when the machine can; Windows uses the system message box (and Windows Hello); Linux uses `zenity` or `kdialog`. Which program runs is data in `skills/os-confirm`, run by the per-OS layer | when the chat has no host confirmation |
| `panel_click` | the owner's click on the panel, with the plan hash in the request | always: the last resort |
| `chat_choice` | ONE chat question showing the whole plan (every step, exact command, size and undo), bound to the plan hash; the owner answers in the chat and the model relays it with `mas_answer`. Recorded with `gesture: chat_choice` and `assurance: chat_relay`, and with `relayed_by_model: true`; **never as `os_dialog`**. A failed step or a changed plan (another hash) asks again | only the kinds `approval-policy.json` lists as `chat_choice` (a setup, an undo) |

**Who answers what is data** (`skills/os-confirm/references/approval-policy.json`). The native operating-system dialog is raised by the harness itself ONLY to pair a computer and to store or remove a key. A setup or an undo is one `chat_choice` question per plan. Everything else the owner decides in the chat (his profile, a choice among options, a task's level when it is unclear, closing an environment) is a closed question answered with `mas_answer`; it writes nothing on a machine. A kind the file does not list keeps the mechanisms its channel offers (one question per step). If a dialog for the same item is already open the harness does not raise another one or time out the first: it says it is open.

**The authority rule (hard).** An answer to a writing step counts as the owner's only when it comes with the human gesture that made it. The coordinator's front door for a run's answers takes a `gesture` (`panel`, `panel_click`, `os_dialog`, `host_dialog`, `host_elicitation`, `cli_typed`, `chat_choice`) that only trusted code sets (the panel's own route, the core after a confirmation); an answer that merely says "owner" is refused (`owner_authority_refused`) and the decision stays open. The operator's answer is recorded apart (CLAUDE-CODE) and never runs a step. A chat model cannot approve a setup step or an undo step by any door it has: `decisions` refuses it, the chat's token cannot reach the panel's routes, an OS-dialog ticket cannot be completed by the caller.

Rules, each with a test that fails when the rule is removed:

* `POST decisions` refuses anything that writes (`confirmation_required`), whoever asks and whatever the option.
* A completion with a plan hash that is not the ticket's, or not the intent's *current* hash, is rejected (`plan_hash_mismatch`); a ticket for an earlier question cannot answer the next one (`stale_decision`).
* A ticket belongs to the channel that opened it, works once, and expires.
* A channel with a model in the middle (every provider chat and terminal agent) never lists `panel_click`, `host_dialog` or `cli_typed` (HA2).
* An accepted confirmation is an `OWNER` event with `channel`, `mechanism`, `plan_hash`, `ticket`. A model's tool call produces none.

**Strength of the OS confirmation.** A dialog with only buttons could be pressed by an agent that controls the screen. So when the machine can verify its owner, that proof is **required after the button**: LocalAuthentication on macOS, Windows Hello on Windows (UNTESTED). Where the machine offers none, the button alone is accepted and the answer is **labeled weaker** in the dialog, the ticket (`assurance: weak`) and the OWNER event; an elicitation or a chat's own dialog is recorded as `host_ui`.

### The text a human approves is the command that runs

A plan's placeholder `<mas>` (the machine's own Multi Auto Setup folder) is an angle-bracket word: Open WebUI stripped it as an HTML tag, and `rm -rf <mas>/downloads/x` reached an owner as `rm -rf /downloads/x`, a delete from the disk root. Every owner-facing surface now shows **`MAS_HOME`** instead, with the line "MAS_HOME is Multi Auto Setup's own folder on that machine." The plan and its hash keep the original text; the mapping is one way to one word and reversible. A confirmation whose command, undo or summary still holds angle brackets is **refused** (it is never shown). An undo labels its paths **Removes**, and a setup step labels them **Writes to**. HA9 and per-host tests hold every host to this: Claude Desktop's chat text and its confirmation form, the OS dialog, Open WebUI's dialogs and the owner panel each show text that, taken back to the plan's words, equals the command in the plan the node is sent, and that survives a host which strips tags. The real resolved path is not shown: the coordinator does not know the node's folder, and a path with a user name would go to a cloud chat.

## Live activity

Every machine's activity reaches the owner's chat as lines: analysis probes, setup steps (sent, running, done or failed, with the size and the time), server start and stop, the minimal call, and tasks the owner asked for outside any run. Each line names the machine, what is happening and who did it:

```
[MODEL] Lenovo · node_gpu: ok, 2 facts (chosen by MODEL, run by GUARD)
[GUARD] Lenovo · preparing step 3 of 6 «Download the model file ...»: sent to the machine · 1.1 GB
[OWNER] Lenovo · you answered approve_run_3: approve
```

* **Merged:** a tool call is one line (who chose it, who ran it, how it went), not three events; a setup step says each state that matters once.
* **Rate-limited:** ordinary lines wait for a 2-second gap per run; an important line (a question, a decision, a failure, the end of a run) releases everything waiting at once; one flush shows at most six lines, and a longer burst merges its ordinary lines into one that says how many and who did each. **The final state of a run is always shown.**
* **Per host:** Claude Desktop and terminal agents call the tool `mas_activity` (new lines since the last call, each once); Open WebUI shows lines in its status line and also the lines of other machines' tasks while a run goes; the owner panel lists the same stream under "Live activity" as a fallback.
* A `confidential` run's lines are withheld from a cloud channel like any intent.
* Download progress is reported by state and total size, not by bytes: the node does not stream byte counts yet.

## Sensitivity

A cloud channel (`claude-desktop`, `terminal`) never receives a `confidential` intent (it gets "withheld"), cannot create a confidential run, and cannot request a confirmation for one. Internal data to a cloud channel passes a filter (fingerprints, addresses and paths dropped), a size cap, and is logged once as `cloud_egress`. A local chat (Open WebUI with a local model, `terminal-local`) receives everything its sensitivity allows.

## Connecting a chat is a setup too

`mas connect [host]` (at the owner's terminal), the panel's "Connect a chat" button, the core goal `connect_host` (and the tool `mas_connect`) start a run that detects which chats are on this computer, asks "Which chats do you want to use Multi Auto Setup in?" with only those, builds a plan from data (`skills/connect-recipes`, every path and flag quoted from the tool's own documentation), shows it as text first, and runs each step only after a human gesture. `mas disconnect host` (goal `disconnect_host`, tool `mas_disconnect`) undoes it in a new run. Hosts: `openwebui` (pinned install in its own folder with `uv`, loopback and offline, our function registered through Open WebUI's API, license exception row shown), `claude-desktop` (our entry merged into `claude_desktop_config.json`; refused while Claude Desktop runs), `claude-code` (`claude mcp add` and `claude mcp remove`, user scope), `codex` (a table in `~/.codex/config.toml`), `opencode` (an entry under `mcp` in `~/.config/opencode/opencode.json`), `muse-code` (an entry under `mcpServers` in its `settings.json`, with the required `schema_version`), and `openwebui-cleanup` (stops and moves aside a manual Open WebUI install from an earlier session, with its own plan; nothing is deleted). The whole plan text, like every approval, is held to the angle-bracket rule: a plan that would show a word a chat strips is refused. A settings file is backed up first, other entries are never read out or touched, and a cell the documentation does not cover refuses with a typed reason (OpenCode and Muse Code on Windows today). Status: SIMULATED-LOCAL (temporary home, fake commands, a stand-in for Open WebUI's server); **no recipe has been run on a real configuration yet** (UNTESTED). The Open WebUI admin password is kept in `~/mas-openwebui/admin.secret` (mode 600) so that the undo can delete the function; moving it to the OS keychain is planned. The manual steps below remain as the fallback and as what the recipes do.

## Connect Open WebUI by hand (a local chat)

Install Open WebUI in its own environment (Python 3.11 or 3.12) and run it bound to loopback, for example:

```bash
mkdir -p ~/mas-owui/data && cd ~/mas-owui/data
OFFLINE_MODE=true ENABLE_VERSION_UPDATE_CHECK=false ENABLE_OLLAMA_API=false ENABLE_OPENAI_API=false \
ENABLE_COMMUNITY_SHARING=false DATA_DIR="$HOME/mas-owui/data" \
  ~/mas-owui/.venv/bin/open-webui serve --host 127.0.0.1 --port 8080
```

`serve` defaults to `0.0.0.0` and writes its secret key into the current folder, hence the explicit host and the `cd`. Offline mode stops the embedding-model download (Open WebUI's document search then does not work; the Pipe does not use it). Create the admin account yourself in the browser. Then Admin Panel, Functions, new function: paste `envrec_harness/openwebui_pipe.py`, save, turn it on, and set the valve `STATE_DIR` to the coordinator's state directory. "Multi Auto Setup" appears in the model list. Open WebUI runs the function in its own process: the coordinator's loopback screen is reachable from it only if both run on the same computer. Open WebUI's own license (BSD-3-style plus a branding clause, not an MIT/Apache/BSD/ISC license) applies to Open WebUI; nothing of it is bundled or modified here.

What the Pipe does: a menu in the chat's own dialog (see the network, analyze, choose the AI, prepare, undo), the network map and cards as embeds that are replaced when something changes, each line of activity in the status line, and every write in the chat's own confirmation dialog, answered by the person clicking. Open WebUI also calls a Pipe for background tasks (the chat title, tags), without dialogs; those calls declare nothing, so they cannot erase what the chat can do.

## Connect Claude Desktop by hand (a provider chat)

Claude Desktop connects a local server as a stdio entry in `claude_desktop_config.json` (Settings, Developer, Edit Config), then restart (the app must be fully quit while the file is edited):

```json
{"mcpServers": {"multi-auto-setup": {"command": "<python of the project's environment>", "args": ["-m", "envrec_harness.mcp_server", "--state-dir", "<the coordinator's state directory>"]}}}
```

The coordinator must run with `--web`. The model-visible tools are `mas_network`, `mas_start`, `mas_prepare`, `mas_undo`, `mas_connect`, `mas_disconnect`, `mas_status`, `mas_activity`, `mas_answer` and `mas_request_approval`; `mas_live` and `mas_view` are app-only (visibility `["app"]`, never offered to the model). The adapter writes what the handshake declared to `<state>/coordinator/harness/handshake-<channel>-<client>.json` and every method the host called to `rpc-<channel>.jsonl` (never arguments).

## Connect a terminal agent by hand

A terminal agent uses the same adapter. Register the stdio command in the agent's own MCP configuration (see the agent's documentation for how), with the channel:

```
<python> -m envrec_harness.mcp_server --state-dir <the coordinator's state directory> --channel terminal
```

Use `--channel terminal-local` when the agent's model runs on this computer (it then sees what a local chat sees). A terminal shows tool results as text, so everything above works as words: the plan text, the numbered questions, the activity lines. A write is confirmed by an elicitation form if the agent's handshake declares one, and otherwise by the OS dialog on the owner's desktop; the owner can always approve on the panel. Status: SIMULATED-LOCAL with a scripted host; no real terminal agent was run (UNTESTED), so which of them declare elicitation is not claimed here: the handshake decides at run time and is written to the handshake file.

## Cards

Self-contained HTML: system fonts, inline SVG, inline CSS, no inline `style` attribute, no URL, no script from outside (HA5 and tests). Every word that comes from a machine or a model is escaped. Visual language: navy and cream; teal = the data stays on the machine; amber = it goes to the cloud; a lock = confidential. The map draws each machine as its own icon (laptop, desktop, server, old PC; the owner says which, because no probe can), the coordinator in the center, animated lines while a task runs, a status dot (online, busy, offline), and what each machine is doing now with its actor; a click opens the machine's card. **Cards are not model context:** the card HTML travels in the tool result's `_meta` (`mas/cards`), kept out of `structuredContent`, which the chat's model reads. Per host: `openwebui` embeds report `iframe:height`; `mcp` is a static shell (`ui://mas/cards`, MIME `text/html;profile=mcp-app`) speaking the MCP Apps `postMessage` protocol; `panel` is framed by the owner's screen from two same-origin files.

## Known gaps and limits

- **Step limit exists, spend limit does not.** A board may escalate a failed subtask a fixed number of steps without asking (`Services.board_preapproved`, default 2, never above the router's maximum, shown in `mas_board_show`). Nothing limits the cost of those steps, and no amount is tracked. The limit is the coordinator setting `board_escalation_limit` (integer, default 2, clamped to 0 up to the router's maximum, a non-integer refused with `bad_type`), read into `Services.board_preapproved`.
- **The sandbox wrapper trusts the code it runs** (`docs/ROUTER_ACCEPTANCE_AMENDMENTS.md`, last section): a deliberately malicious solution could forge the result line of an exam or of a project's tests. Documented, not fixed.

* **Cards do not appear in Claude Desktop for a local (stdio) server (known gap, 2026-10-30).** In the real session both of the host's clients (`claude-ai`, `local-agent-mode-…`) declared `io.modelcontextprotocol/ui` with `text/html;profile=mcp-app`. The chat client called `tools/list` and `resources/list`; the tool call came through the agent client; **neither ever called `resources/read` for `ui://mas/cards`**, before or after the call, so the view was never fetched. Our tool list passes the schema checks in the host's own code (`_meta.ui.resourceUri` and the legacy `ui/resourceUri`, a `ui://` URI under 2048 characters). In the host's code, showing MCP Apps for local servers sits behind its own feature flags, which a server cannot see or set. We do not work around it: every tool result's text stands on its own, and we never ask the chat's model to paint a card in text. **Recheck after a Claude Desktop update:** a `resources/read` row in `<state>/coordinator/harness/rpc-claude-desktop.jsonl` is the sign.
* **Diagnosing a view that does not appear.** (1) The view must complete `ui/initialize` and report a non-zero height; `tests/js/host_sim.mjs` runs the real script against a host simulator. (2) Read `rpc-<channel>.jsonl`: is there a `resources/read` with the uri? (3) `harness_view_context` events in the coordinator log come from the view itself. (4) The host's console: Claude Desktop, Help, Troubleshooting, Enable Developer Mode, then inspect the inner iframe.
* **A chat with computer use can act outside our tools.** In Claude Desktop the model may offer to install something another way. The harness cannot stop a host from granting its model that access. What we do: every tool result that offers options or an approval states that installs happen only through Multi Auto Setup's setup flow with the owner's approval; writes need a confirmation the model cannot press; and owners should keep the host's computer-use approvals on.
* **A chat's own model may still recommend an option.** The harness returns the options as an unordered set, says the owner chooses and nobody recommends, and puts each option's numbers beside it. What the host's model says on top of that is the host's.
* **MCP Apps cannot tell a click from a model call** (open issues 738 and 746), so no click counts as the owner for a write.
* **Windows and Linux** confirmation dialogs, Windows Hello, and a real terminal agent are UNTESTED. Only macOS has run the node's setup operations; the Windows and Linux code paths exist.
* **Open WebUI's free-text message is not interpreted**: the Pipe offers a menu. A front-desk protocol that lets the local model pick is a decision for the owner.
* **Setup steps ran on a simulated node** (a loopback origin and a fake runtime) in every verification so far; no real model download or real install has been run.
* **Text details not yet polished:** the machine card shows the raw key for a fact it has no label for; run summaries mix our words with the model's.

## Adapt another chat

Only the door changes. A new chat needs: (1) a translator for the calls above; (2) its channel in `CHANNELS` (name, cloud, model in the middle, the confirmations it can offer); (3) a token (created automatically for every non-panel channel); (4) a way to show a card, or the text fallback; (5) its capabilities declared at connect. A chat whose model decides what to call is `model_in_middle`: it never lists `panel_click` or `host_dialog`.

## Status of each part (what was run)

| part | label |
|---|---|
| intents, cards, core rules, tickets, the gesture rule, the OS-dialog flow with a fake dialog | unit tests, SIMULATED-LOCAL (real coordinator and node on loopback) |
| setup, undo, activity through the real MCP adapter (a subprocess) with the simulated node, scripted setup model, loopback origin and fake runtime | SIMULATED-LOCAL |
| Open WebUI: Pipe, menu, cards, analysis with the real `local-9b`, options, pick, prepare plan text, a step approved in the chat's own dialog, undo | **REAL** (Open WebUI); node and setup model simulated |
| Claude Desktop: handshake, tool calls, OS dialog | **REAL**; cards not shown (gap above) |
| terminal agents | SIMULATED-LOCAL; real agent UNTESTED |
| native macOS dialog | **REAL**; the Touch ID prompt itself: UNTESTED in the owner-visible sense |
| Windows message box and Hello, Linux `zenity`/`kdialog` | UNTESTED |
| the panel's activity list and approvals | SIMULATED-LOCAL (tests read the page's script and the state it draws) |


## Pairing and unpairing from a chat (`mas_pair`, `mas_unpair`)

Two thin tools over what the coordinator already does for the terminal and the owner screen. The chat can ask for a code and read the state; it can never complete a pairing or an unpair. Both end in a
`confirm` intent bound to a plan hash (the computer's name, system, architecture and key fingerprint for a pairing; the computer for an unpair) that the owner answers with a gesture of their own (the system
dialog, the host's own dialog, the owner panel). Only that answer runs the action (`on_decision`, called from the same `_resolve` that records the OWNER event). `mas_answer` on such a confirmation is refused
(`confirmation_required`); `complete_confirmation` on a system-dialog ticket is refused (`not_host_answered`); a plan that changed while the dialog was open invalidates the ticket (`plan_hash_mismatch`).
The 6-digit code is NOT in any tool result: a chat's model (a cloud model for Claude Desktop) never reads it. The coordinator shows it to the owner in a dialog of the owner's own computer (`notices` in `skills/os-confirm/references/dialogs.json`: macOS `osascript`, Windows a message box, Linux `zenity` or `kdialog`; the text goes as a separate argument or an environment variable, never through a shell) and the tool result says only that it is on their screen. The dialog is shown once per code (again only if the owner closed it) and taken down when the code is used. When the computer cannot show a dialog the result says so and points to the owner screen or `mas pair`; it never falls back to the chat. The code is the password of one key exchange: about two minutes, one try, never in a log or an event, and useless without the owner's approval in the confirmation. What the chat's model needs to know about when and how to call them is the skill `skills/harness-app-chat`, delivered
as the MCP server's `instructions`.
