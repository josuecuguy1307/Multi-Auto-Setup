# PROGRESS.md — lab notebook (newest last)

Each entry: what was done, what failed, what is next. Failed attempts stay here.

## 2026-11-16/17 — Session 1: Phase 1 recon and harness

**Recon**
- Host: Windows 11 Home, Python 3.13 via `py`/uv (PATH `python` is 3.8), uv 0.13.6, Docker 29.2.1, WSL, Node 22, git 2.47. Missing: `gh`,
  QEMU, Renode, KiCad (to run in containers). The session runs the desktop app's Claude Code 2.1.325; PATH `claude` is 2.1.79.
- No local clone of MAS existed (only the installed node in `~/.mas`, untouched). Cloned `josuecuguy1307/Multi-Auto-Setup` (35 commits,
  HEAD 5cb6c2e) to `C:\Users\Josue\code\Multi-Auto-Setup`, branch `device/v0`.
- MAS: ~43k lines in `src/`, ~36k in tests. Read: PROJECT_OVERVIEW, KNOWN_GAPS, PROVISIONING_SAFETY, the approval path
  (`envrec/tools/ctx.py` prepare_write/run_tool), policy, undo executor, `envrec_net/envelope.py`. Not yet read end to end: the coordinator,
  board/router, skills library (≈100 skills), harness app. Note: MAS ships a `lan-sweep` skill — must be excluded from the device build.
- Repo already has Apache-2.0 (MISSION 8.1 is outdated on this).

**Failed / surprising**
- First baseline run used `--timeout`, which this project does not have (it uses its own `envrec_timeout` plugin): exit 4. Re-ran.
- Baseline MAS suite on Windows: **8 collection errors**, run interrupted — `termios`, `fcntl` (POSIX-only), `time.tzset` (POSIX-only),
  and `ImportError: device_name_hits from test_tools` (2 conformance modules import from a tests module). MAS was developed on macOS/Linux.
  Not fixed (out of scope; D-013). The device suite runs separately (`device/tests`).
- First advisor consultation (Fable 5.1, earlier session) did not finish before the session ended. Re-run on Fable 5.2 after the Claude
  Code update: done (DECISIONS A-1).

**Harness**
- `scripts/device_accept.py` oracle + `device/acceptance/criteria.yaml` (16 criteria, phases 1–6, all FAIL at start), its own tests
  `device/tests/test_harness.py`; hooks (`scripts/device_hook.py`: session-start, commit gate, stop auto-commit); subagents `evaluator`,
  `reviewer`; `run.sh` loop (container mode not built yet); CLAUDE.md, DECISIONS.md, ACCEPTANCE.md, NEEDS_HUMAN.md, STEER.md.

**First unit: signed approvals (P1-UNIT)**
- Wrote tests first for a JSON token with `issued_at` and a persisted counter. Before any code, the advisor's review changed the spec
  (binary body, gate-held nonce, gate-clock TTL, signed summary, undo hash, classes). Rewrote the tests to that spec; ran them: **red**
  (collection error, module missing). Then wrote `src/mas_device/approval.py` and `keymodel.py`: 53 passed on the first run.
- Two test edits during build, both to the test data and not to weaken a check: `{"x": True}` removed from the "ambiguous values" list
  (JSON booleans are unambiguous), and the "extra field" mutation now uses a string so it still exercises `action_mismatch`.

**Next**
- Reviewer pass on DECISIONS.md/ACCEPTANCE.md; evaluator on P1-UNIT; commit red-then-green; push `device/v0`.
- Then: container harness image (`device/harness/Dockerfile`), saboteur-patch check in the oracle, Phase 2 gate (P2-GATE).

## 2026-11-17 — Session 1, end (usage limit reached)
- Red commit 20d4779 proven red in a clean worktree (collection error); green commit 19bc4ca (53 approval tests pass).
- Oracle, phase 1: P1-HARNESS PASS (24 tests). P1-DOCS FAIL (reviewer section R-1 missing). P1-UNIT FAIL (evaluator report missing).
- The evaluator and reviewer subagents were started (custom agents load only in a new session, so they ran on the built-in Plan agent)
  and were **stopped unfinished** at the usage limit. Re-run both first next session; save outputs to device/evals/evaluator/P1-UNIT.md
  and DECISIONS.md "## R-1 Reviewer pass".
- Push of device/v0 failed silently (credential prompt cannot show in this shell): nothing is on the remote. NEEDS_HUMAN #4.
- Docker Desktop not running (daemon pipe missing): container harness blocked. NEEDS_HUMAN #7.
- P2-LOG: tests written and seen red, then src/mas_device/log.py: 19 pass. Not yet run through the oracle or the evaluator.

## 2026-11-17 — Session 2
- Push of device/v0 works now (GCM sign-in done by Josué in the terminal). Docker Desktop launched, but the engine was not up when checked.
- Reviewer R-1 (fresh context): 8 must-fix items, recorded verbatim in DECISIONS.md. A new P1-DOCS test keeps P1-DOCS FAIL until each has a
  resolution with evidence. M8 resolved (run.sh advisor opt-in, tested). M1–M7 OPEN.
- Evaluator P1-UNIT run 1: **FAIL** (clock rollback / NaN TTL fail open, optional class check, empty undo accepted, unbounded pending,
  malformed input escaping as non-Rejected, key-model counter issues, 10 surviving mutants). Report: device/evals/evaluator/P1-UNIT.md. Not fixed yet.
- P2-GATE: tests red (e772bda), then gate.py: 44 pass, oracle PASS. Evaluator run 1: **FAIL** (case-insensitive duplicate paths lose the
  undo; caller-object swap after consume writes unapproved bytes; concurrent _run not serialised; 6 surviving mutants). The oracle PASS is
  provisional: the unit is not closed. Report: device/evals/evaluator/P2-GATE.md. Not fixed yet.
- P2-LOG evaluator run 1: **FAIL** (rewrite past a known head verified ok; U+2028 broke the log for good; same-size edit then append;
  no known head on reopen; two writers forked the chain in 128/200 runs). Fixed: tests for every item (red 00ea4d4), then log.py rewritten
  (ASCII byte-exact lines, known-head check at any length, full re-verify under an OS lock before each append, open(expected=...),
  explicit repair_partial). 41 pass, oracle PASS. Evaluator run 2 not done yet. Criterion retitled: it no longer claims the model's process
  cannot write the log (that is P2-GUARDRAILS).
- Failed attempt: my first test edits used `Path.write_text`, which writes CRLF on Windows, so 8 old tamper tests were tampering line endings
  instead of the field they meant to; switched the helper to bytes. A same-size edit pattern (`"n": 1,`) never matched (n is the last key).
- STEER (Josué, in chat): move to the hardware work now. Next: firmware core in C (Phase 4) and PCB as code (Phase 5). The P1-UNIT / P2-GATE
  fixes listed above stay open.
- Firmware (Phase 4, host build): advisor Fable 5.2 wrote the core spec (DECISIONS A-2). Toolchain: zig cc from PyPI `ziglang==0.13.0` in
  runs/toolchain (Docker engine still down). Monocypher 4.0.2 vendored unmodified; its Ed25519 matches `cryptography` byte for byte.
  Tests first (red e85e233), then the C core: 3-opcode allowlist, COBS+CRC16 framing, press-after-request latch, strict press window at the
  press time, two-page counter log committed and read back before signing, key page never touched by an opcode. P4-FW-HOST oracle PASS,
  296 tests (60 differential scripts vs keymodel.py, power-cut sweeps incl. page rollover with 3 erase variants, first-boot cut, 3 saboteur
  builds; correction after evaluator run 1: two of the saboteur tests then only showed the saboteur misbehaving, now they run the suite's
  own check and require it to fail). SIMULATED. Evaluator run 1: FAIL (counter rollback after 513 failed commits, key regenerated after
  signing, tick wrap). Fixed test-first (red a20a48e): 308 tests pass, D-015. Evaluator run 2 pending.
- Failed attempts: a 300-byte payload test expected `malformed` but the spec says RX overflow is `bad_frame` (test corrected); the
  printable-check saboteur left `printable()` unused and -Werror refused to build it (saboteur now keeps the call). A combined shell command
  that also ran a no-op `sed -i` on a vendored file was blocked by the auto-mode classifier; redone as plain copy + Write.
- Blocked for Phase 5: KiCad (kicad-cli ERC/DRC, D-009) needs the Docker engine or a native install (NEEDS_HUMAN #7, #8).
- Firmware evaluator run 2: FAIL (a torn first-boot key write could pass the CRC and brick a never-used key; 5 NV guards untested; no B→A
  test). Fixed test-first (red 2c2607b, green e6d3cbd, D-016): 317 tests, oracle PASS. Run 3 started.
- PCB (Phase 5): KiCad 10.0.8 installed per-user by winget (Josué approved). Advisor spec A-3. Route: design.py → gen_sch.py (.kicad_sch,
  ERC 0 with every severity) → kicad-cli netlist → gen_pcb.py under KiCad's Python (board from the netlist, own A* grid router).
  Failed attempts, in order: Fable's button footprint name was wrong (SW_SPST_PTS645 → SW_SPST_PTS645Sx43SMTR92); the stock U585 LQFP48
  symbol has no VDDUSB pin (pin 36 is VDD: flagged UNVERIFIED); I split USB D± into connector-side and MCU-side nets although the USBLC6
  is flow-through (ERC caught it: isolated labels); ERC lib-table warnings until a .kicad_pro existed; parts overlapped on 20×48 mm (board
  now 20×54); 27 parity "missing field" until symbol fields were copied to footprints (`FOOTPRINT.SetField`, `GetField` in v10, not
  `GetFieldByName`); the stock USB4105 land pattern itself violates 0.25 mm hole clearance (0.194 mm) → one scoped custom rule, listed for
  human review; NPTH clearance in my router was 0.05 mm short; first routing pass failed USB D±, LED and 3 MCU ground pins → rip-up/retry
  passes; MCU ground then SWD → MCU ground vias moved under the chip.
- PCB done to the gate: fully routed (19/19 nets) after rip-up passes and MCU ground vias under the chip; generation made deterministic
  (`pcbnew.KIID.SeedGenerator`: KiCad saves items ordered by UUID); the deleted-track saboteur first picked a 0.15 mm stub inside a pad,
  then a redundant GND stub — it now removes every I2C_SDA track. **P5-PCB oracle PASS, 20 tests (DESIGN-ONLY).** Renders sent to Josué.
- Firmware evaluator run 3: FAIL (press during the display redraw approved the new request; no minimum time on screen; TRNG and key-write
  failures untested). Fixed test-first (red 5747266, D-017): draw first, then clear the latch; 1 s minimum dwell in firmware and keymodel;
  every Python test that pressed at 0 ms now waits the dwell. P4-FW-HOST 323 tests PASS; P2-GATE/P2-LOG re-run PASS. Run 4 pending.
- Emulation (P4-FW-EMU): QEMU 11.1.0 installed (winget). Same core cross-compiled for Cortex-M33 with zig; boots on mps2-an505 and answers
  over the CMSDK UART. Failed attempts: freestanding build had no string.h (added a minimal one); the Bash tool collapses `\` in heredocs,
  which put a NUL byte and broken f-strings into build.py (fixed with file tools; use Write/Edit for code with backslashes); the first
  transcript test compared mostly refusals (1 signature in 20 scripts) and missed a frozen clock — fixed with approval flows and a saboteur.
  27 tests pass. Renode split into P4-FW-RENODE (FAIL until installed).
- Renode 1.17.0 portable (approved by Josué) runs the same an505 ELF on its stock MPS2 TrustZone platform: P4-FW-RENODE 4 tests.
- Software findings round (Josué: "fix software findings"): P1-UNIT runs 1-2, P2-GATE runs 1-2, P4-FW-HOST run 4 and all 7 reviewer R-1
  must-fixes resolved test-first (D-020, D-021, R-1 resolutions). Oracle rewritten: hermetic pytest, `confirm` (fresh worktree at HEAD) as
  the loop's only stop condition, saboteurs in criteria (8, all killed), min_tests, timeouts, oracle identity and criterion in the
  fingerprint, status/verify read-only, 9 new criteria with pinned thresholds. P1-DOCS now PASS. Failed attempts: ini `pythonpath` resolves
  relative to the ini file, so the first hermetic runs and worktree/saboteur copies imported the installed code (fixed with PYTHONPATH =
  the tree's own src); the oracle's default arguments captured module constants at definition time (status wrote the real ACCEPTANCE.md
  from a test); a test fixture named src/code.py shadowed the stdlib `code` module once src came first on the path; `git show` output
  decoded with the console code page returned None. Evaluator runs 3 (P1-UNIT, P2-GATE) next; then the board evaluator's findings.
- Session end (usage limit): P1-UNIT evaluator runs 3-4 and P2-GATE run 3 fixed test-first and pushed (52ab72b). P1-UNIT passes every test
  and saboteur; it needs an evaluator PASS whose Code-Hash is 386ca88241a01442e0b434d1c51039b692f0866916aeb969a4a0c6aa161438ff (re-run
  the evaluator at 52ab72b). P2-GATE evaluator run 4 was still running at the limit (its report, if any, is not recorded). NEXT: P1-UNIT
  evaluator run 5; P5-PCB evaluator findings (device/evals/evaluator/P5-PCB.md: per-pin decoupling incl. pin 24, USB D± on F.Cu only,
  pcbnew silk-stamp check, pin-level netlist tests, .kicad_dru content test, fab equals a fresh build, HUMAN_REVIEW fixes); then Phase 2 units.
- Board evaluator findings fixed (P5-PCB 35 tests PASS): fifth 100 nF, every MCU supply pin has its own capacitor within 4 mm (2.3-3.7 mm);
  USB D+/D- top layer only with no vias, routed after the MCU ground and the SWD lines (which take the bottom layer to cross it); 6 nets
  cross under USB on B.Cu, a declared deviation listed for review (a B.Cu keep-out under USB made 6 nets unroutable: the USB pins sit on
  the MCU's right side, so nets beyond them must cross). Tests now read the board through pcbnew (stamp, layers, distances) and the
  netlist pin by pin; every decoupling cap removed is caught. Failed attempts: the keep-out; USB first then SWD (SWD could not cross);
  KiCad 10 suffixes netlist pinfunctions with the pin number (PA12_33); float equality on capacitor values.
- Gate evaluator run 6: rollback lost update, pre-fsync compare window, code-point path lengths: fixed (D-024), P2-GATE 137 PASS.
  P1-UNIT run 6 fixed (nonces across fresh interpreters); evaluator run 7 pending.
