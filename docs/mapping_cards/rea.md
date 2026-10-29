# Mapping card: REA (rea-agents 6.7.0, MIT, repo commit d87e73f2)

Built from real runs on macOS 15.7.1 / Apple M3 / Node 22.20.0. Raw outputs: `fixtures/software/rea-output/` (see `RECORDED.md`). Adapter: `src/envrec/adapters/rea.py`.

**Verdict: lossy.** Usable for the software side with three static commands. Not usable here for native decompilation.

## Tool to ToolSpec

| REA command | Our tool | Tier | Target kind |
|---|---|---|---|
| `analyze-javascript-application <dir> --json` | `analyze_js_app` | read | app (directory with package.json) |
| `inspect-macho <file> --json` | `inspect_binary` | read | app (Mach-O file) |
| `inspect-artifact <file> --json` | `inspect_artifact` | read | app (any file; directories are refused by REA) |

Nothing else is reachable (no capture, scenario, adb, debugger, browser, extract, Frida).

## Output field to Fact key

| REA field | Fact key | Status rule |
|---|---|---|
| package node `name` / `version` / `main` | `app.name`, `app.version`, `app.entrypoint` | tested if node is `observed`, else hypothesis |
| `javascript-asset` node labels | `app.source_files` | tested if every label has an observed/exact node |
| `javascript-module` labels not in the app's files | `app.external_modules` | same rule; real data: both `node:http` nodes are inferred/low, so hypothesis |
| `endpoint` node labels | `app.endpoint_candidates` | hypothesis when inferred |
| macho `format`, `selected_architecture.name` | `app.format`, `app.arch` | tested |
| macho `dependencies` (exhaustive: true) | `app.dependencies` | tested |
| macho `imports`/`exports` (exhaustive: false) | `app.imports_seen`, `app.exports_seen` | tested, limitation "not exhaustive" |
| artifact `subject.digest.sha256`, `subject.format` | `app.sha256`, `app.format` | tested |
| artifact `unexplored_branches` (REA's own unknowns) | unknown fact `app.functions` + `Unknown` | unknown |
| `confidence` (categorical) | `Evidence.confidence` float | exact 1.0, observed 0.95, derived 0.8, high 0.85, medium 0.6, inferred 0.5, low 0.3; unmapped labels 0.5 plus a flag |
| `limitations[]`, `evidence_id`, provider id/operation | `Evidence.limitations`, `note`, `probe` | copied (max 6, 200 chars each) |
| whole JSON | `raw_ref` (sha256) | never inlined |

## What is lost
- Everything inside REA's 594 KB graph beyond the fields above (edges, per-module relationships, AST statistics).
- Route tables: REA found none of the app's three served routes.
- REA's coverage for imports/exports is "not exhaustive"; we say so but cannot complete the lists.

## What REA got wrong (measured against fixtures built by us)
- It reports `welcome`, a key into an in-memory Map, as a network endpoint (inferred, low). Kept as a hypothesis; scores wrong.
- It tags `main.js` as `electron-main` (inferred, high) although the app is plain Node. The adapter does not use that node.
- Two REA tools agreeing on a file's format are not independent sources, so they never make a fact `verified`.

## Blocked or unavailable on this machine
- Native deep analysis (Ghidra, Hopper, IDA, Kuna): not configured; `rea doctor` reports `healthy: false`.
- `inspect_sqlite_database`: needs Node 24.15+ (we have 22.20).
- JADX/Android, adb, firmware, browser and capture tools: not wired, not tested.
- Docker daemon is down, so nothing Linux-only was tried.

## Safety notes
- `sandbox-exec` cannot launch `ps`, which REA's cleanup needs (`cleanup_incomplete / spawn EPERM`), so no OS-enforced network ban. Mitigation: command allowlist, human-configured path roots, isolated `HOME`, empty cwd, and a measured check: 0 network sockets in 4 to 20 samples per command. This is observation, not proof.
- The published package has one install hook (`prepare`, a no-op outside a repo checkout); no dependency has install scripts. Installed with `--ignore-scripts`.
