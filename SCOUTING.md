# SCOUTING

Read from clones in `scout/` (git-ignored, read-only, nothing executed). Licenses are from each repo's own LICENSE file. Star counts ignored.

## Verdict table

| Item | Link | License (LICENSE file) | SHA | Verdict | Files / pattern | Contract impact |
|---|---|---|---|---|---|---|
| REA | https://github.com/morluto/rea (npm: `rea-agents` 6.7.0) | MIT | d87e73f2 | **tool, used in M10** (CLI, not transplanted) | `analyze-javascript-application <path> --json` returns application graph + Evidence; Evidence carries `evidence_id`, subject, provider, authority, confidence, `limitations`, `coverage.scope/status` (`complete`/`unknown`); `record_unknown` / `update_unknown` tools | Our Evidence gets `provider`, `confidence`, `limitations`; ProbeResult gets `coverage` + `unknowns[]`. Mapping in `docs/REA_ALIGNMENT.md` |
| buspirate-mcp | https://github.com/mplogas/buspirate-mcp | MIT | 36589d5d | **design-only** (rewrite) | `src/buspirate_mcp/safety.py`: 3 tiers (read-only / allowed-write / approval-write) as a static tool-name→tier dict, 86 lines | Four tiers (observe, read, write_logged, write_approval) live on `ToolSpec.tier`, not in a name table; policy adds authorization, generated-tool and denylist checks |
| smolagents | https://github.com/huggingface/smolagents | Apache-2.0 | 96f33faa | **skip** | none | none; never replaces contracts/policy/store/verifier |
| Pydantic AI | https://github.com/pydantic/pydantic-ai | MIT | e3aba7ad | **skip** | none | none; plain Pydantic v2 is enough |
| BootstrapAgent | https://github.com/Vossera/BootstrapAgent | MIT | a5ea466b | **transplant later** (verifier phase) | `code/src/rethink/verifier/verifier.py`, `.bootstrap` package contract, Docker verifier under `code/` | `Skill` carries `signature`, `code_ref`, `tests_ref`; verifier call sees evidence + test results only |
| Voyager | https://github.com/MineDojo/Voyager | MIT | 55e45a88 | **design-only** | `voyager/agents/skill.py` (skill library), `critic.py` (verifier role) | `Skill.verified_runs`; skills enter the menu at rung 1 |
| Repo2Run | https://github.com/bytedance/Repo2Run | Apache-2.0 | 65042aa2 | **transplant later** (sandbox phase) | `build_agent/docker/` (Docker sandbox with rollback) | `ToolSpec.origin=generated` runs only in the sandbox |
| open_deep_research | https://github.com/langchain-ai/open_deep_research | MIT | 1b7d2e80 | **design-only** (swarm phase) | `src/legacy/multi_agent.py` (487 lines, supervisor + parallel workers) | none now; workers share the fact store |
| RepoLaunch | https://github.com/microsoft/RepoLaunch | MIT | 099857f3 | **not evaluated** (license read only) | `launch/` | none |
| awesome-mcp-hardware | https://github.com/beriberikix/awesome-mcp-hardware | Apache-2.0 | 50e72818 | **reference list** | README: 21 servers. It does NOT list es617, bmdragos, quantumnic, KenosInc or hardware-mcp, contrary to the research log | none |
| ble-mcp-server | https://github.com/es617/ble-mcp-server | MIT (GitHub API; LICENSE file not read) | not cloned | **design-only** (Ring 1 later) | not read | none now |
| serial-mcp | https://github.com/bmdragos/serial-mcp | MIT (GitHub API) | not cloned | **design-only** | not read | none now |
| arduino-mcp-server | https://github.com/hardware-mcp/arduino-mcp-server | MIT (GitHub API) | not cloned | **design-only** | not read | none now |
| mcpd | https://github.com/quantumnic/mcpd | MIT (GitHub API) | not cloned | **design-only** | not read | none now |
| sigrok-mcp-server | https://github.com/KenosInc/sigrok-mcp-server | MIT (GitHub API); sigrok data license unconfirmed | not cloned | **design-only** | not read | none now |
| instrument-mcp | **not found** (searched GitHub) | unknown | n/a | **skip** | n/a | none |
| Octopus Protocol | https://arxiv.org/abs/2605.09055 | paper | n/a | **design-only** | n/a | n/a |
| CAI, GhidraMCP | not touched | research-only / none stated | n/a | **never use** | n/a | n/a |

## Key findings

1. **REA is far bigger than the notes said** (package `rea-agents` 6.7.0; native decompiler providers, Frida, Android, firmware and many more). Most of it needs Hopper, Ghidra, IDA, adb or Linux. The one path that needs none of those is the **JavaScript application analysis** (`analyze-javascript-application`), which is what M10 will use on an app we write ourselves.
2. **REA's Node requirement is `^22.23.4 || ^24.22.1 || >=26.12.1`; this machine has 22.20.0.** Install may warn or refuse. M10 tests this honestly and marks REA blocked if it will not run, without upgrading Node globally.
3. REA's own lifecycle scripts in the repo are only `prepare` (husky, repo checkouts only), `prepack`, `prepublishOnly`. The published package's install scripts are checked from the registry before M10 installs anything.
4. buspirate-mcp's tier model is a static name→tier dict. Useful idea, not worth transplanting: our tiers sit on ToolSpec and policy has more inputs.
5. The research log's claim that awesome-mcp-hardware lists the BLE, serial, Arduino, mcpd, sigrok and instrument servers is wrong. Those repos exist (except instrument-mcp) but are not in that list.
6. Docker CLI is installed, daemon is down. The sandbox (rung 5) is outside this phase anyway.

## Update after M10
The Node warning above was about repo HEAD. The published rea-agents 6.7.0 accepts Node 22.19+, so it runs on 22.20.0. Verdict for REA: tool, lossy (see docs/mapping_cards/rea.md).
