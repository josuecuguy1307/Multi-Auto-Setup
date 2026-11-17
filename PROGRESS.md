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
