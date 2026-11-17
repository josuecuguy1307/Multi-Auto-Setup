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
