# Router environments: license, non-interactive mode, execution safety (T4a, STOP 1)

Date of reading: 2026-10-30. Labels: **verified by code** = a program read it (GitHub API, LICENSE file bytes at the commit, `--help` on this Mac); **declared by the project** = quoted from the vendor's documentation (URL given); **UNTESTED** = nothing was run. Nothing below was executed against a model; no environment was installed.

## 1. The table

| | Claude Code CLI | Codex CLI | OpenCode | Pi |
|---|---|---|---|---|
| Subtype | provider (subscription / API) | provider (subscription / API) | own harness | own harness |
| Counts as for sensitivity | cloud | cloud | local (only with the loopback llama.cpp backend) | local (same) |
| Upstream | vendor page; closed source | `openai/codex` | `anomalyco/opencode` (the old `sst/opencode` redirects to it) | `badlogic/pi-mono`, which GitHub reports as `earendil-works/pi` |
| Pin | none (we never install it). Seen here: 2.1.285 | none. Seen here: 0.159.0 (latest release `rust-v0.166.0`, 2026-10-29, commit `67f0994b`) | commit `173f6c6dce7db669679625f6af93651ae66f16be` is the HEAD of `dev`; a **release** pin is needed for the recipe (latest `v1.19.5`, 2026-10-28) | commit `35dfa757378bd7a8570670e99c2687bdd1039a06` (`main` HEAD); latest release `v1.4.0` (2026-10-21). npm name differs by source (`@mariozechner/...` vs `@earendil-works/pi-coding-agent`); the recipe must pin by registry integrity hash, not by name |
| License | closed / commercial. Not MIT or Apache-2.0, so **we never install, bundle or copy it** (contract 9.4); used only as the owner's already-installed, signed-in tool. Terms for automated use: **to review** | **Apache-2.0**, verified by code (LICENSE bytes at `67f0994b`, GitHub SPDX `Apache-2.0`). We still do not install it: it is a provider | **MIT**, verified by code (LICENSE at `173f6c6d`, "MIT License, Copyright (c) 2025 opencode"; SPDX `MIT`) | **MIT**, verified by code (LICENSE at `35dfa757`, "MIT License, Copyright (c) 2025 Mario Zechner"; SPDX `MIT`) |
| Non-interactive mode | `claude -p "<prompt>"` with `--output-format json` (`text`, `json`, `stream-json`), `--max-turns N`, `--no-session-persistence`, `--model` (declared: https://code.claude.com/docs/en/cli-reference) | `codex exec` with `--json`, `-o/--output-last-message <file>`, `-C/--cd <dir>`, `-s/--sandbox <mode>`, `--skip-git-repo-check`, `--ephemeral` (declared: https://learn.chatgpt.com/docs/developer-commands?surface=cli; the flag names also verified by code in `codex exec --help` of 0.159.0 on this Mac) | `opencode run [message..]` with `--format json`, `--dir <path>`, `--model provider/model`, `--auto` (declared: https://opencode.ai/docs/cli/) | `pi -p` / `--print` (final text to stdout), `--mode json` (JSONL), `--no-session`, `-t/--tools <list>`, `-nt/--no-tools`, `--model`, `--provider` (declared: `packages/coding-agent/docs/cli.md` at the pinned commit) |
| Signed-in check (presence only, never a token) | `claude auth status`: JSON, exit 0 logged in / 1 not (declared; run here: `loggedIn: false, authMethod: none` -> this Mac would give `not_signed_in` today, **REAL**) | `codex login status`: prints the mode, exit 0 when logged in (declared; run here: "Logged in using ChatGPT" -> `signed_in`, **REAL**; the mode is read, nothing else) | not applicable (local backend, no key) | not applicable |
| Model backend | the vendor's | the vendor's | llama.cpp via `@ai-sdk/openai-compatible`, `options.baseURL: "http://127.0.0.1:<port>/v1"`, set through `OPENCODE_CONFIG_CONTENT` or a config in the workspace (declared: https://opencode.ai/docs/providers/, llama.cpp example) | llama.cpp via `models.json` `{"providers":{"llama-cpp":{"baseUrl":"http://127.0.0.1:<port>/v1","api":"openai-completions","apiKey":"<dummy>","models":[{"id":"..."}]}}}` (declared; the docs' example uses `localhost`, ours must be the literal `127.0.0.1`, N5; a dummy key is needed for the model to appear, and is not a secret) |
| Needs | network to vendor; signed-in session | network to vendor; signed-in session | Node/Bun runtime or its release binary (to verify in the recipe step); no Docker, no GUI | Node.js >= (to verify); no Docker, no GUI |
| License of what the recipe would download | n/a | n/a | recipe downloads a release asset; its SHA-256 and the asset's own license are read at recipe time | same, plus the npm tree's licenses (a transitive non-permissive dependency would refuse it) |

Not found / not verified (said plainly): the current text of Anthropic's and OpenAI's terms on automated use of the subscription CLIs (so `automated_use = to review` for both: shown as options, **never auto-routed**, contract 9.4 / spec 10.4); OpenCode's and Pi's runtime requirements and release assets; whether `opencode run` blocks on an `ask` permission (its docs do not say, so the design never uses `ask`).

## 2. Execution safety (hard limit): what each tool lets us switch off

The rule: a coding environment runs only in a per-task workspace directory and either (a) its own tool settings disable command execution **and** confine writes, or (b) it runs inside our sandbox backend; otherwise `sandbox_missing`. Writes outside the workspace are refused.

| | (a) tool-side switch | Confines writes to the workspace by the tool itself? | Mode I propose | Without a sandbox backend |
|---|---|---|---|---|
| Claude Code | `--tools "Read,Write,Edit"` (no `Bash`), `--disallowedTools Bash` as a second line, `--permission-mode dontAsk` (no prompt can hang the run; declared), cwd = workspace | Declared yes by permission rules (writes outside the working directory need approval, which `dontAsk` denies). **UNTESTED**, and I rely on the vendor's word | **tool-side (a)**, no `--add-dir`, plus a stand-in test that a write to `../x` fails | runs in (a) with no execution; it cannot run the code it writes, which is fine for the "write code" task |
| Codex CLI | no way to remove its shell tool. Its own OS sandbox: `--sandbox workspace-write` (writes limited to the `--cd` dir) or `read-only`; approvals `on-request`/`never` | Declared yes, by an OS sandbox (Seatbelt on macOS, Landlock/seccomp on Linux per its docs, **UNTESTED** here; Windows: not verified) | **tool's own OS sandbox**, `-s workspace-write -C <workspace>`. **Decision for the owner:** does Codex's own sandbox count as "inside the existing sandbox"? It runs commands, just confined. If the owner says no, Codex is `sandbox_missing` unless our Docker backend runs it whole | refuse on Windows (`sandbox_missing`) until its sandbox is verified there; macOS/Linux: the owner decides above |
| OpenCode | `OPENCODE_PERMISSION='{"bash":"deny","webfetch":"deny","external_directory":"deny","edit":"allow"}'` (keys and values declared: https://opencode.ai/docs/permissions/); never `ask` | Declared: `external_directory` covers paths outside the project; **UNTESTED** | **tool-side (a)**, workspace as `--dir`; stand-in test for a write outside | (a) holds: no execution |
| Pi | `--tools read,edit,write` (drops `bash`). Pi **states it has no permission system or sandbox** (repo README at the pinned commit) | **No**: `write`/`edit` take any path the user can write. The tool cannot confine them | **inside our sandbox backend only** (Seatbelt / bubblewrap / Landlock+seccomp / Docker, contract 8). Its config and caches pointed inside the workspace (8.4) | **`sandbox_missing`**. Today the only existing backend is Docker; macOS Seatbelt and the Linux ones are "planned" in the contract, and Windows is open |

Consequences to confirm at STOP 1:

* On this Mac today (Docker present): Pi can run (in Docker); OpenCode and Claude Code run in mode (a); Codex waits for the owner's decision above.
* A node with no sandbox backend (e.g. the Lenovo, Windows, until Docker or a backend exists) can run **OpenCode in mode (a)** and the plain llama.cpp text/code adapter, but **not Pi**.
* In mode (a) the environment cannot execute the code it writes. Tasks whose grader must run that code are graded by the coordinator or in our sandbox, never by the environment itself.

## 3. Dispatch design (needs your confirmation because it amends the contract)

The node today cannot start any program from a task: T1 says its allowlist is one dict literal and `tasks.py` imports nothing that can start a program, and the only write task is `setup_step` (3.9). Running a CLI is a new capability, so I propose **one new task type, `environment_run`**, defined the same way as `setup_step`:

* **Closed recipe as data** (`skills/router-environments/references/environments.json`): for each environment and OS family, the executable's name, the argument template, the permission/config payload, the env-var NAMES it may receive, the wall-clock limit. No flags in Python (H1).
* **Token scope**: environment id, workspace id, the task's sensitivity, the payload reference, the model endpoint, and the route decision's ref. The node refuses on a mismatch (`bad_scope`), on a missing sandbox when the mode needs one (`sandbox_missing`), or when the recipe's hash differs (`stale_recipe`).
* **Workspace**: created by the node under its own `<mas>/workspaces/<task_id>/` through the per-OS layer's `WriteScope`; the payload is copied in by reference; the result (diff or text) is read back as a task result with evidence. After the task the workspace is removed unless the owner keeps it.
* **No owner approval per task** (that is for `setup_step`), but the environment must have been **installed and opened by the owner** (the draft's open subtypes, plus the install recipes of T3a's format for OpenCode and Pi, each write approved as in W4). Provider CLIs are never installed by us.
* Result: `result` with `evidence` and label `tested` only for what code measured (exit code, files changed, grader result). The model's own summary is stored as `hypothesis`.
* New conformance: W-env, same shape as W1/W2 (the coordinator sends it only through its own function; the node authorizes before accepting and before running; no read-only handler reaches it). It also forces the existing T1/W3 static checks to keep passing, which is the part I most need to see fail first.

This reuses the same signed channel, opens no listening port and adds no dependency.

## 4. Dispatch modes and coverage (as built, 2026-10-30)

**Dispatch mode per environment** (data in `skills/router-environments/references/environments.json`; `confinement` is what keeps the tool inside its workspace):

| Environment | `text` task | `code` task | Needs our sandbox backend |
|---|---|---|---|
| Local model (llama.cpp) | HTTP chat to the loopback server, no tools (`none_needed`) | same | no |
| Claude Code | `--tools ""` + `dontAsk` (`tool_declared`) | `--tools Read,Write,Edit` + `--disallowedTools Bash` + `dontAsk` (`tool_declared`) | no |
| Codex | `-s read-only` (`unverified`) | `-s workspace-write` (`unverified`) | **both: yes** (owner decision 2) |
| Muse Code | `exec` (`unverified`) | `exec` (`unverified`) | **both: yes** (no flag documented) |
| OpenCode | `OPENCODE_PERMISSION` bash, webfetch, external_directory, edit all `deny` (`tool_declared`) | same with `edit: allow` | no |
| Pi | `--no-tools` (`sandbox_required`) | `--tools read,edit,write` (`sandbox_required`) | **both: yes** |

Codex: no flag that removes its shell tool was found, so even `read-only` (its own OS sandbox) is not trusted; both modes run only inside our backend.

The sandbox wrapper exists (`policy.json` -> `sandbox_wrappers.docker`, tested as an argument list: the tool's key travels by NAME, never as a value in an argument), but **no image is built and nothing ran inside Docker** (UNTESTED). On this Mac Docker is present; on the Lenovo there is no backend, so Pi, Muse Code and Codex are `sandbox_missing` there. Inside the backend the workspace is mounted at `/work` and `{workspace}` in the arguments is `/work`; that mapping is covered by an argument-list test only.

**Coverage** (what actually ran; "stand-in" = an executable that mimics the documented interface and refuses any other argument list; it proves our adapters, not the vendors' tools):

| | macOS arm64 | Windows x86_64 | Linux arm64 / x86_64 |
|---|---|---|---|
| Router, rule, sensitivity, filter (pure) | SIMULATED-LOCAL | UNTESTED (pure Python, no OS call) | UNTESTED |
| `environment_run`: node + coordinator over real TLS on loopback | SIMULATED-LOCAL | UNTESTED | UNTESTED |
| Local model adapter (HTTP chat) | SIMULATED-LOCAL (httpx mock of the server; no real llama.cpp call) | UNTESTED | UNTESTED |
| Claude Code, OpenCode adapters | SIMULATED-LOCAL (stand-in) | UNTESTED | UNTESTED |
| Codex, Pi, Muse Code adapters (sandbox only) | argument list and wrapper only (stub runner); no process, no Docker | UNTESTED | UNTESTED |
| Real vendor tools | **not run**. On this Mac: `codex` 0.159.0 signed in, `claude` 2.1.285 not signed in, `opencode`/`pi`/`muse` not installed | UNTESTED | UNTESTED |

Windows: the executable names are data per family; `find_exe` uses the OS lookup (executable extensions included); the process runner is `subprocess` with an argument list; none of it has run on Windows. Linux: same.

## 5. Decisions taken at STOP 1 (2026-10-30) and how they are applied

1. `environment_run` approved as a contract amendment, built like `setup_step`; owner-created or owner-approved tasks only.
2. Codex's own OS sandbox does **not** count as the existing sandbox until we verify it ourselves (a write outside the workspace and a network call both blocked). **Restated at STOP 2: zero execution outside our sandbox, for every task type.** No flag that removes Codex's shell tool was found, so both modes (`read-only` for text, `workspace-write` for code) are `unverified` and need our sandbox backend; without one the task is `sandbox_missing`. A data change only. Recorded in the catalog as `confinement: unverified`.
3. Actor tags: ROUTER for route decisions, GUARD for refusals.
4. Tie margin `0.1`, visible config (`policy.json`).
5. **Muse Code (Meta)** added as a provider environment with two accesses. **Settled at STOP 2 (2026-10-30): both key names are real, each where it applies.** Meta's official docs: the Meta Model API key is `MODEL_API_KEY` (https://dev.meta.ai/docs/getting-started/authentication; `ai.developer.meta.com/docs` redirects to `dev.meta.ai/docs`); the Muse Code CLI in non-interactive environments reads `META_API_KEY` (https://dev.meta.ai/docs/muse-code/auth). The catalog now detects the api-key access when EITHER name exists (names only, never values) and lets both names through to the tool by name. Not documented: whether the CLI accepts a key set only as `MODEL_API_KEY`. The paragraph below was written before this and is kept for the record. **Discrepancy to confirm:** the decision names the env var `MODEL_API_KEY`, but Meta's official Muse Code docs say `META_API_KEY` ("Muse Code checks `META_API_KEY` first, then a stored key, then a stored browser session", https://dev.meta.ai/docs/muse-code/auth). `MODEL_API_KEY` appears only in third-party pages (OpenClaw's provider docs and musecodes.io, an unofficial site). The catalog uses **`META_API_KEY`** (name only, never the value), declared by the project; if you meant `MODEL_API_KEY` it is a one-line data change. Other facts (declared, https://dev.meta.ai/docs/muse-code): headless `muse exec "<prompt>"`; installer `curl -fsSL https://dev.meta.ai/install.sh | sh` (macOS/Linux) or `irm https://dev.meta.ai/install.ps1 | iex` (Windows); macOS, Linux, Windows. **Not found:** any `muse exec` flag (output format, working directory, tool or sandbox switches), any signed-in status command (the docs mention only `/login` inside a session and `muse logout`), the CLI's license, the MCP configuration beyond a mention of `~/.config/muse/settings.json` in one search summary. **What is missing for Muse Code (revisit when Meta testing starts):** any non-interactive flag beyond `muse exec "<prompt>"` (output format, working directory, model, permission or sandbox switches), a session-status command (so the plan access is stated by the owner), the CLI's license, and MCP configuration details. Consequences: signed-in state cannot be detected by command, so the plan access is `not_signed_in` unless the owner states it; no flag to disable execution is documented, so its confinement is `unverified` for BOTH task types and it only runs inside our sandbox backend (stricter than Codex, whose read-only text mode needs none); no flag beyond `exec` is used; license unknown, so it is never installed by us; terms `to review`. Counts as cloud.
6. Pins (resolved 2026-10-31; nothing installed, nothing downloaded):
   - **OpenCode v1.19.5** (published 2026-10-28), tag commit `810c0eba1a7348036f8bdf61e6632d0c36c6876c`; LICENSE re-read at that commit: MIT. Asset SHA-256 as GitHub reports it for each release asset (`GET /repos/anomalyco/opencode/releases/tags/v1.19.5`, field `digest`); **we did not download or recompute them**: the recipe step must check the downloaded bytes against them.

     | OS / arch | Asset | SHA-256 | Bytes |
     |---|---|---|---|
     | macOS arm64 | `opencode-darwin-arm64.zip` | `c1c9e62521192192ef4c3ed1355ada2144055bddb61682217e997b03c55203c1` | 64278924 |
     | macOS x64 | `opencode-darwin-x64.zip` | `b0212efa6db347187225a17fa3a31c6b261bf4c9f1589a290b0ca902d720d250` | 69100888 |
     | Linux x64 | `opencode-linux-x64.tar.gz` | `588310a336fa3b9df36b3f0225b26f679d453efaad0cf3d66c3651ef3ec1647a` | 79651896 |
     | Linux arm64 | `opencode-linux-arm64.tar.gz` | `fae5be262d693b7c5e562856cc473e59e741772936db0c0600ac0d5ed5bd088b` | 78513091 |
     | Windows x64 | `opencode-windows-x64.zip` | `09a6e693157f0887e504f2cf4357a8cbddc5ead5d0dffb9a269dcc47c48640f5` | 81738227 |
     | Windows arm64 | `opencode-windows-arm64.zip` | `5d49139bd917511ff35a3c06065c9f47918346668f05ea020e778286f302fbb7` | 75773358 |

     Baseline-CPU (`-baseline`) and musl variants exist and are not pinned here; Intel Macs and older x64 CPUs may need the baseline build (the recipe chooses by measured facts). The binary's runtime requirements were not read.
   - **Pi 1.4.0**: npm package **`@earendil-works/pi-coding-agent`** (published 2026-10-21). The repo `badlogic/pi-mono` redirects to `earendil-works/pi`, and the older package name `@mariozechner/pi-coding-agent` stops at 0.73.1 and is not the current one. Tag `v1.4.0`, commit `4a080252f06caf63a525d7873c5d94d3c23887e0`; LICENSE re-read at that commit: MIT (the registry also says MIT). Integrity (registry `dist.integrity`): `sha512-Z41LpZ3yYHSC/UUps6Mr39UMFPwH8elhJBG9DY1P7kuzsE5qSK/6eMj2EHzO5evJRQWLbKSqUMw+4nCNtIyiQg==`, shasum `7f5dd2a4f7fa46ed252465ac4d341831896b1fc2`, tarball `https://registry.npmjs.org/@earendil-works/pi-coding-agent/-/pi-coding-agent-1.4.0.tgz`. It needs Node >= 22.19.0 and has 22 direct dependencies whose licenses (and the transitive tree) are **not checked yet**: the recipe is not accepted until they are.
7. Conservative option for the remaining two questions: (a) the draft's open subtypes default to **closed** for every provider and own-harness environment until the owner opens them (the owner can open all); (b) OpenCode and Pi may be refused with a typed reason when a release asset, runtime or license does not fit; nothing is installed by this session.
