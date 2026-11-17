# CLAUDE.md — MAS portable device (branch `device/v0`)

**Start with `HANDOFF.md`** (where things stand, what is running, gotchas). Source of truth: `MISSION.md`. Every session reads, in order: `MISSION.md`, `STEER.md`, the last entry of `PROGRESS.md`, `DECISIONS.md`, `NEEDS_HUMAN.md`.
If `AGENT_STOP` exists: commit, add a note to `PROGRESS.md`, end.

## Rules that bite
- MISSION.md section 4 is fixed. Nothing is "done" without its test PASS in `test-results.json`, and only `scripts/device_accept.py` writes that file.
- Work on `device/v0` only. Small commits, tests before each commit (a hook refuses `git commit` while a PASS is stale). Never merge to `main`, never release, never push elsewhere.
- Inspection and setup code runs only against fixtures, containers and VMs you create. Never the local network, never real machines.
- Third-party code: read before running, run in a container. No credentials anywhere (repo, logs, prompts).
- Label honestly: SIMULATED (Python model), EMULATED (QEMU/Renode/VM), DESIGN-ONLY (PCB), REAL (ran on real hardware). Log failed attempts in `PROGRESS.md`.

## Where things are
- `src/mas_device/` — device code (policy gate, key protocol, log, portable runtime). `src/envrec*` — existing MAS.
- `device/tests/` — device tests. `device/acceptance/criteria.yaml` — criteria → tests → covered files.
- `device/firmware/`, `device/hardware/`, `device/medium/`, `device/evals/`, `device/paper/` — as phases reach them.
- `.claude/agents/evaluator.md` (fresh context, read-only, FAIL by default), `.claude/agents/reviewer.md` (advisor stand-in).

## Commands (Windows: `.venv/Scripts/python`; POSIX: `.venv/bin/python`)
- Fast cycle: `python -m pytest -q -p no:cacheprovider device/tests`
- Oracle: `python scripts/device_accept.py run --id P1-UNIT` · `... run --phase 2` · `... status` · `... verify`
- Unit cycle: test FAIL → build → oracle PASS → `evaluator` subagent (save its report to `device/evals/evaluator/<ID>.md`) → commit.
- Loop: `bash run.sh` (fresh `claude -p` sessions until every criterion is PASS or `AGENT_STOP` exists).
- Existing MAS suite: `python -m pytest -q tests conformance` (on Windows 8 modules fail to collect: POSIX-only imports; see PROGRESS.md).

Output hygiene: print little, send logs to `runs/` (git-ignored), use `ERROR` as the grep-able marker.
