# HANDOFF.md — traspaso de contexto (para continuar en otra cuenta o sesión)

> Written 2026-11-19 (third handover, same machine, another Claude account). Branch `device/v0`, everything committed and pushed.
> Read this first, then `MISSION.md`, `STEER.md`, the last entries of `PROGRESS.md`, `DECISIONS.md` (D-001..D-030, A-1..A-3, R-1) and
> `NEEDS_HUMAN.md`. `CLAUDE.md` has the working rules; `device/README.md` explains the device and how to reproduce every check.
> Español abajo en resumen. The repo is the source of truth; this file only tells you where things stand and what bites.

## 1. Where things stand: 14/26 PASS (oracle: `.venv/Scripts/python scripts/device_accept.py status`)

| Criterion | Status | Independent evaluator |
|---|---|---|
| P1-HARNESS | PASS 42 | (the oracle under test) |
| P1-DOCS | PASS 18 | reviewer R-1 resolved |
| P1-UNIT | PASS 179, 4 saboteurs | run 7 PASS (closed) |
| P2-GATE | PASS 149, 2 saboteurs | run 9 findings fixed (D-029); **run 10 not yet run** |
| P2-LOG | PASS 44, 2 saboteurs | run 3 PASS |
| P2-GUARDRAILS | PASS 36, 6 saboteurs | run 2 FAIL (credential oracle) fixed (D-030); **run 3 not yet run** |
| P4-FW-HOST / EMU / RENODE | PASS 328 / 27 / 4 | evaluated in earlier rounds |
| P5-PCB | PASS 53 (DESIGN-ONLY) | run 4 findings fixed (D-027 addenda); **run 5 not yet run** |
| P0-SECRETS | PASS 17 | **never evaluated** (built 2026-11-18) |
| P4-SECBOOT | PASS 9, 2 saboteurs (DESIGN-ONLY) | **never evaluated** (`device/firmware/SECURE_BOOT.md`) |
| P6-REPO | PASS 8, 1 saboteur | **never evaluated** (`device/README.md`) |
| P5-ENCLOSURE | PASS 16, 2 saboteurs (DESIGN-ONLY) | **never evaluated** (`device/hardware/enclosure.py`) |
| P2-PAIRING, P2-MEMORY | FAIL (not built) | next software units; no outside help needed |
| P2-MODEL | FAIL (not built) | needs Josué's OK to download a local model |
| P2-PORTABLE, P2-ZEROTRACE, P2-FLEET, P3-READONLY | FAIL (not built) | **blocked: need working Docker or VMs** (NEEDS_HUMAN 7) |
| P3-INSPECT, P3-REPORT, P3-NOCREDS | FAIL (not built) | can be built against fixtures |
| P6-EVALS, P6-PAPER | FAIL (not built) | last |

## 2. How to resume (in order)

1. `cd C:\Users\Josue\code\Multi-Auto-Setup`, `git pull`, check `git status` is clean and you are on `device/v0`.
2. `.venv/Scripts/python scripts/device_accept.py status` and `... verify` (both read-only; verify takes ~1 s now).
3. Choose with Josué: either run the pending evaluators (§1, bold) or build the next units (P2-PAIRING, P2-MEMORY). Each evaluator run
   costs ~65k-270k tokens; several rounds in a row now find only small test gaps, so building may be the better use of budget.
4. Unit cycle: red test commit -> implementation -> oracle PASS -> evaluator (fresh context) -> fix test-first -> commit -> push.

### Running evaluators (what worked)
- The `evaluator` agent type only appears when the session STARTS inside this repo. Prompt: unit, run number, commit, spec entries, the
  proportionality rule below, "work in a `git archive HEAD | tar -x` copy under `runs/eval/<dir>/tree`, write only there, do NOT run
  `device_accept.py run` (other evaluators run in parallel), write the report to `runs/eval/<ID>.runN.report.md` AND return it".
- If a usage limit kills them mid-run, resume each one with SendMessage (its work so far is kept) instead of starting over.
- Save each report verbatim to `device/evals/evaluator/<ID>.runN.md` with a one-line header (unit, run, date, commit). That folder is
  covered by P1-UNIT, so re-run P1-UNIT after adding reports.
- Proportionality rule (it ended endless tails of tiny findings): BROKE only for paths that violate MISSION 4.1 (or, for the board, real
  design errors); in-process misuse that fails closed, declared limits and equivalent mutants are NOTES; an untested guard whose removal
  fails OPEN is a WEAK TEST and blocks PASS.

## 3. Rules and habits that matter here

- Nothing is "done" without the oracle's PASS; only `scripts/device_accept.py run` writes `test-results.json`. The stop condition is
  `device_accept.py confirm` (clean tree, fresh worktree at HEAD, everything re-run).
- A project hook refuses `git commit` while any recorded PASS is stale: run the stale criteria first (`verify` names them).
- Advisor: Fable 5.2 via the Agent tool with `model: fable` (A-1..A-3). Push only `device/v0`; never merge to main, never release.

## 4. Gotchas that cost time (read before editing)

- **P0-SECRETS covers the whole repo (`.`)**: ANY edit anywhere (even PROGRESS.md or HANDOFF.md) makes it stale. Re-run it last, just
  before committing (`run --id P0-SECRETS`, ~15 s).
- **Editing `scripts/device_accept.py` makes EVERY PASS stale** (the oracle's own hash is in every fingerprint): then run
  `device_accept.py run` for everything (~40 min, mostly the board).
- Adding any file under `device/firmware` makes P4-FW-HOST/EMU/RENODE and P4-SECBOOT stale; under `device/hardware`, P5-PCB (~15-20 min)
  and P5-ENCLOSURE; under `device/`, P6-REPO; under `src/mas_device`, P2-GATE/LOG/GUARDRAILS.
- **The Bash tool collapses `\\` to `\`** in commands and heredocs: regexes and `\n` in patch scripts get corrupted (it happened again
  three times this session). Write patch scripts with the Write tool into the scratchpad and run them.
- Don't run two `device_accept.py run` at once. Long runs: start them in the background and wait for the notification.
- The oracle runs pytest hermetically with `PYTHONPATH=<tree>/src`. Never name a test fixture module like a stdlib module.
- Windows: `git show` output needs `encoding="utf-8"`; `Path.write_text` writes CRLF (use bytes for byte-exact files).
- Board: `gen_pcb.py` is a home-made grid router. USB D± are routed as fenced chains through U2 (D-027); the board takes 5-10 min to build;
  debugging harnesses that draw ASCII maps of the routes are in `runs/hw/` (git-ignored). KiCad 10 suffixes netlist pinfunctions with the
  pin number (`PA12_33`).
- Guardrail battery: the daemon runs through a launcher with an audit hook (`device/tests/test_guardrails.py`); it is a detector for
  careless weakening, not containment (D-028/D-030).

## 5. Tools on this machine (not in git; reinstall if you change machines)

| Tool | Where | How it got there |
|---|---|---|
| Python venv | `.venv/` | uv; `.venv/Scripts/python` |
| zig cc 0.13.0 (firmware) | `runs/toolchain/` (git-ignored) | `uv venv runs/toolchain --python 3.13` + `uv pip install --python runs/toolchain/Scripts/python.exe ziglang==0.13.0` |
| Renode 1.17.0 portable | `runs/toolchain/renode/renode_1.17.0-portable/` | official zip from github.com/renode/renode releases v1.17.0 |
| KiCad 10.0.8 | `%LOCALAPPDATA%\Programs\KiCad\10.0` | `winget install KiCad.KiCad` |
| QEMU 11.1.0 | `C:\Program Files\qemu` | `winget install SoftwareFreedomConservancy.QEMU` |
| GitHub push | Git Credential Manager | signed in once by Josué |
| Docker | not working (engine never came up) | blocks the VM/container criteria |

## 6. Open items for Josué (`NEEDS_HUMAN.md`)

Docker or VMs (blocks 4 criteria), OK to download a local model (P2-MODEL), hardware license (CERN-OHL-P-2.0 proposed), confirm v0
compute = host CPU, secure-boot review on a sacrificial STM32U585 (item 10), human PCB and enclosure review
(`device/hardware/HUMAN_REVIEW_PCB.md`), buying boards, a real two-machine test, arXiv submission.

---

## Resumen en español

- Rama `device/v0`, todo subido. **14 de 26 criterios pasan.** Hechos en esta cuenta: P0-SECRETS (sin credenciales en el repo ni en
  su historia), P4-SECBOOT (diseño de arranque seguro, solo diseño), P6-REPO (`device/README.md`), P5-ENCLOSURE (carcasa paramétrica,
  solo diseño), más varias rondas de evaluadores con sus correcciones (D-027 a D-030).
- Al retomar: `git pull`, `device_accept.py status`. Luego, con Josué: o bien correr los evaluadores pendientes (sección 1, en negrita),
  o seguir construyendo P2-PAIRING y P2-MEMORY. Cuatro criterios necesitan Docker o máquinas virtuales que funcionen.
- Cuidado: P0-SECRETS cubre todo el repo, así que cualquier cambio lo deja "stale": correrlo al final, justo antes del commit. La
  herramienta Bash corrompe las barras invertidas: escribir los scripts de edición con Write. No correr dos `run` del oráculo a la vez.
