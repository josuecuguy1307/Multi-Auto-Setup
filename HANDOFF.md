# HANDOFF.md — traspaso de contexto (para continuar en otra cuenta o sesión)

> Written 2026-11-17 at commit f2ad136 (branch `device/v0`, pushed). Read this first, then `MISSION.md`, `STEER.md`, the last entries of
> `PROGRESS.md`, `DECISIONS.md` (D-001..D-024, A-1..A-3, R-1) and `NEEDS_HUMAN.md`. `CLAUDE.md` has the working rules and commands.
> Español abajo en resumen. The repo is the source of truth; this file only tells you where things stand and what bites.

## 1. Where things stand (oracle: `python scripts/device_accept.py status`)

| Criterion | Status | Notes |
|---|---|---|
| P1-HARNESS | PASS 41 | the oracle under test |
| P1-DOCS | PASS 18 | reviewer R-1: all 8 must-fixes resolved with evidence |
| P1-UNIT | PASS 179, 4 saboteurs | closed: evaluator run 7 VERDICT PASS, Code-Hash fc884e03… (commit a99fcbc) |
| P2-GATE | PASS 141, 2 saboteurs | run 7 findings fixed (D-026, e2d6045); **evaluator run 8 not yet run** |
| P2-LOG | PASS 44, 2 saboteurs | run 2 findings fixed (D-026); **evaluator run 3 not yet run** |
| P2-GUARDRAILS | PASS 26, 5 saboteurs | built 385e4a6 (D-025: `mas_device/gated.py`); **evaluator run 1 not yet run** |
| P4-FW-HOST / EMU / RENODE | PASS 328 / 27 / 4 | firmware in C; QEMU mps2-an505 and Renode run the same ELF |
| P5-PCB | PASS 35 (DESIGN-ONLY) | **board evaluator run 2 FAIL** (`device/evals/evaluator/P5-PCB.run2.md`): USB D- bypasses the ESD part U2 (stub), 31 vias inside SMD pads (one on U1 pin 36); tests miss the power path (U3 EN, LDO cap values, VDDA 1 uF), button path (R5/C11), SWD pin order and D1 polarity; HUMAN_REVIEW line about the USB-C body is wrong. Not yet fixed |
| everything else | FAIL (not built) | P2-PORTABLE, P2-ZEROTRACE, P2-FLEET, P2-MODEL, P2-MEMORY, P2-PAIRING, P3-*, P4-SECBOOT, P5-ENCLOSURE, P6-*, P0-SECRETS |

Updated 2026-11-17 (second account), 10/26 PASS. Evaluators to run next (fresh context; give them the proportionality rule of §3;
tell them to work in a `git archive HEAD` copy under `runs/eval/` and to write their report to `runs/eval/<ID>.runN.report.md` so it
survives a usage limit): **P2-GUARDRAILS run 1**, **P2-GATE run 8**, **P2-LOG run 3**. P5-PCB run 2 is done (FAIL, see table): fix its 4 REQUIRED
items test-first (reroute D- through U2, vias out of pads, netlist tests for power/button/SWD/LED), then board evaluator run 3. The `evaluator` agent type only loads when the session starts in this repo;
otherwise paste `.claude/agents/evaluator.md` into a general-purpose agent and tell it not to run `device_accept.py run`.

## 2. How to resume (in order)

1. `cd C:\Users\Josue\code\Multi-Auto-Setup`, `git pull`, check `git status` is clean and you are on `device/v0`.
2. `.venv/Scripts/python scripts/device_accept.py status` and `... verify` (both read-only).
3. Re-run the evaluators of §1 with the `evaluator` subagent (fresh context, read-only). Save each report to
   `device/evals/evaluator/<ID>.runN.md`. **Only when P1-UNIT's evaluator says PASS**, append its block (with the `Code-Hash:` line
   computed by `python -c "import sys; sys.path.insert(0,'device/tests'); import test_unit_cycle as t; print(t.judged_hash())"`) to
   `device/evals/evaluator/P1-UNIT.md` so its last `VERDICT:` is PASS, then `device_accept.py run --id P1-UNIT`.
4. Fix any finding test-first (red commit, then green commit), re-run the oracle for the touched criteria, commit, push `device/v0`.
5. Next units, in the mission's order: Phase 2 (guardrail battery, pairing incl. D-021 consent, portable launch, zero-trace, fleet,
   model, memory), then Phase 3 (inspection), P4-SECBOOT (doc), P5-ENCLOSURE, Phase 6 (evals, paper), P0-SECRETS.

## 3. Rules and habits that matter here

- Nothing is "done" without the oracle's PASS; only `scripts/device_accept.py run` writes `test-results.json`. `status`/`verify` write nothing.
  The loop's stop condition is `device_accept.py confirm` (clean tree, fresh worktree at HEAD, everything re-run).
- Unit cycle: red test commit -> implementation -> oracle PASS -> evaluator (fresh context, FAIL by default) -> fix -> commit -> push.
- Give evaluators this proportionality rule (it ended an endless tail of tiny findings): BROKE only for paths that violate MISSION 4.1;
  in-process-only misuse that fails closed, declared limits and equivalent mutants are NOTES; an untested guard whose removal fails OPEN
  is a WEAK TEST that blocks PASS. Tell them to mutate with `PYTHONPATH=<copy>/src` (the editable install ignores `-o pythonpath`).
- Advisor: Fable 5.2 via the Agent tool with `model: fable` (used for A-1, A-2, A-3). In `run.sh` the advisor is opt-in (`MAS_ADVISOR=fable`)
  because unattended Fable is billed without asking (NEEDS_HUMAN #1).
- Push only `device/v0`; never merge to main, never release, never send anything.

## 4. Gotchas that cost time (read before editing)

- **The Bash tool collapses `\\` to `\`** in commands and heredocs: Python edits with `\n`, `\x..` or regex escapes get corrupted (it once put
  a NUL byte into build.py). Write edit scripts with the Write tool into the scratchpad and run them, or use Edit.
- The oracle runs pytest hermetically with `device/acceptance/pytest.ini` and `PYTHONPATH=<tree>/src` (an ini `pythonpath` resolves relative
  to the ini file and silently imports the installed copy). Never name a test fixture module like a stdlib module (`code.py` broke pytest).
- Don't run two `device_accept.py run` at once: each rewrites `test-results.json` and one overwrites the other.
- Windows: `git show` output needs `encoding="utf-8"`; `Path.write_text` writes CRLF (use bytes for byte-exact files); KiCad files,
  fab outputs and vendored code are `-text` in `.gitattributes`.
- KiCad 10: netlist `pinfunction` carries the pin number (`PA12_33`); footprint fields via `SetField`/`GetField`; seed UUIDs with
  `pcbnew.KIID.SeedGenerator` for byte-identical boards. Board routing takes ~3-6 min per build (`hwbuild.py`); P5-PCB rebuilds it.

## 5. Tools on this machine (not in git; reinstall if you change machines)

| Tool | Where | How it got there |
|---|---|---|
| Python venv | `.venv/` | uv; `.venv/Scripts/python` |
| zig cc 0.13.0 (firmware) | `runs/toolchain/` (git-ignored) | `uv venv runs/toolchain --python 3.13` + `uv pip install --python runs/toolchain/Scripts/python.exe ziglang==0.13.0` |
| Renode 1.17.0 portable | `runs/toolchain/renode/renode_1.17.0-portable/` | official zip from github.com/renode/renode releases v1.17.0 (sha256 18bcf145…b9db) |
| KiCad 10.0.8 | `%LOCALAPPDATA%\Programs\KiCad\10.0` | `winget install KiCad.KiCad` |
| QEMU 11.1.0 | `C:\Program Files\qemu` | `winget install SoftwareFreedomConservancy.QEMU` |
| GitHub push | Git Credential Manager | signed in once by Josué |
| Docker | not working (engine never came up) | not needed so far |

## 6. Open items for Josué (`NEEDS_HUMAN.md`)

Hardware license (proposed CERN-OHL-P-2.0), confirm v0 compute = host CPU (no Pi/Jetson), Docker if containers are wanted, real two-machine
test, buying boards, human PCB review (`device/hardware/HUMAN_REVIEW_PCB.md`), arXiv submission.

---

## Resumen en español

- Rama `device/v0`, todo subido (commit f2ad136). Pasan: arnés del oráculo, docs, compuerta de política, bitácora, firmware (host, QEMU,
  Renode) y la placa (solo diseño). **P1-UNIT** solo espera un veredicto PASS del evaluador con el `Code-Hash` correcto.
- Al retomar: `git pull`, `device_accept.py status`, volver a lanzar los evaluadores de la sección 1, corregir con pruebas primero,
  y seguir con la Fase 2 (batería de guardrails, emparejamiento con consentimiento D-021, arranque portátil, rastro cero, flota).
- Cuidado: la herramienta Bash corrompe las barras invertidas (`\\`); escribir los scripts de edición con Write. No correr dos `run` del
  oráculo a la vez. Las herramientas (zig, Renode, KiCad, QEMU) están en esta máquina, no en git: sección 5 para reinstalarlas.
