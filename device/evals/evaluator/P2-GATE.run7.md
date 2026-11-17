# Evaluator report — P2-GATE, run 7 (2026-11-17, evaluator agent, fresh context). Evaluated commit a99fcbc. Verbatim.

UNIT: P2-GATE
VERDICT: FAIL
RAN: (commit a99fcbc; scripts in runs/eval/p2gate7b/)
- git archive HEAD | tar -x -C runs/eval/p2gate7b/tree
- PYTHONPATH=<copy>/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python -m pytest -c device/acceptance/pytest.ini --rootdir=<copy> -q -p no:cacheprovider <copy>/device/tests/test_gate.py -> 137 passed (mas_device.__file__ checked: it is in the copy)
- scripts/device_accept.py verify + status: live repo shows P2-GATE STALE (uncommitted work, see NOTES); HEAD copy shows P2-GATE PASS
- a1_tokens.py (54): replay, bit flips in all 11 token fields, unpinned key, expiry edges, bad clocks, wrong session, class/undo_hash/summary/press-window swaps, counter regression, token type confusion
- a2_actions.py (98): 27 field mutations of a planned write approved directly through the token gate, forged undos with direct UNDO tokens, WRITE-for-UNDO, parse_action with duplicate keys/NaN/float/NFD/deep nesting/bigint
- a3_paths.py (37): junction, file and folder symlinks, hard link, case variants, 8.3 alias, dotless i, a search for NTFS-vs-casefold aliases, reserved/odd names, >259-unit path, file-vs-folder confusion
- a4_runtime.py: one token from 8 threads, 2 writes on 1 pre-state, undo racing a write, BaseException at 3 points after consume, log tampered/deleted/read-only, corrupt key state, key counter rollback
- a5_rollback.py / a5b_rollback.py: owner save during the rollback, deterministic and with a separate owner process (80 runs)
- a6_pathlen.py, a6b_pathlen_astral.py: path limit at 258/259/260 units, ASCII and astral characters
- a7_misc.py: root swapped for a junction, hard link made after approval, Zone.Identifier ADS, ValueError after consume
- mutate.py + mutate2.py: 93 mutants (85 in gate.py, 8 in approval.py), each in its own copy, PYTHONPATH=<mutant>/src with the import path asserted, test_gate.py run hermetically
- a8_survivors.py, a9_rollback_survivors.py: surviving mutants run against the real tree as a control
- fixcheck/: in a scratch copy, adding a final compare to the rollback closes BROKE 1

BROKE:
1. The rollback still overwrites a save the owner made meanwhile (run-6 item 1 is only half fixed). In `_apply`'s rollback, the code reads `now`, compares it with the bytes the gate wrote, then calls `_replace_file(p, prev)` with NO `expect`. So the temp file's create, write and fsync all happen between the check and `os.replace`. That is exactly the window run 6 item 2 flagged on the forward path. Exact input:
   - Start state: a.txt = b"old-a", b.txt = b"old-b".
   - Approved write: plan_write({"a.txt": b"NEW-a", "b.txt": b"NEW-b"}).
   - b.txt is held open by another handle, so `os.replace` fails (PermissionError, apply_failed) and the rollback runs.
   - The owner saves a.txt = b"OWNER-SAVE" while the rollback's temp file is being fsynced.
   - Result: a.txt == b"old-a", so the owner's save is destroyed, and write.failed has `left_changed == []`, so the log says nothing was left changed.
   - Real timing with a separate owner process: lost in 3 of 80 runs. In the other runs where the owner wrote, its save was left alone and named 26 times and landed after the rollback 3 times.
   This breaks rule 1 (host data changed without its owner's action) and contradicts D-024's "Rollback never undoes the owner". In fixcheck/, `_replace_file(p, prev, expect=_sha(written))` plus treating Refused like OSError keeps the save and names a.txt.

WEAK TESTS:
1. The `_apply` per-file pre-state check (`if _sha(prev) != f["pre"]`) for DELETES. Mutant apply-no-file-pre: all 137 tests pass. Real effect: an approved delete {"etc/app.conf": None}, with the owner editing the file during the write.approved append, deletes the file and the owner's edit is lost. The real tree refuses (pre_state_changed). Only replaces are tested, and for those the `expect` check also catches it.
2. The rollback restore of an overwritten EXISTING file (`_replace_file(p, prev)` changed to `pass`). Mutant rollback-no-restore: all 137 pass. Real effect: a failed 2-file write over existing files leaves a.conf = b"a=2\n" with `left_changed == []` and no undo right. test_failure_midway_rolls_back_and_registers_no_undo only rolls back a newly created file.
3. The rollback re-creating folders that a failed undo removed (`_make_dir(p)` for op rmdir changed to `pass`). Mutant rollback-no-remake-rmdir: all 137 pass. Real effect: the undo of n/a/x.txt fails at rmdir n (a process has its working folder in n). It leaves n/a and x.txt gone, and the report blames "someone else" for x.txt.

LABELS:
- D-024 says "Rollback never undoes the owner ... restores or deletes a file only if it still holds exactly what the gate wrote", and gate.py's rollback comment says "never undo their change". Both are false while the rollback replace has the fsync window (BROKE 1).
- Otherwise ok: SIMULATED appears in the gate.py docstring, the test docstring, criteria.yaml and ACCEPTANCE.md. The junction-swap window, the forward final-compare window, and the ACL/timestamp/ADS limits (D-022) are all declared.

NOTES:
- Held: replay (same action, other action, across sessions, after restart); every token field tampered; a body signed by an unpinned key under the pinned key_id; expiry (119.999 accepted; 120.0, 120.001, -0.001, NaN, inf refused; str/None/bool clocks refused); class, undo_hash, summary and press-window swaps; all 27 action-field tamperings and 4 forged undos refused at execute; junction, symlink and hard-link paths; the root swapped for a junction; case and 8.3 aliases; reserved names and trailing dot/space; the path limit is exact (259 written, 260 refused, counted in UTF-16 units including astral characters plus the 18-unit suffix); one token from 8 threads gives 1 success; of two writes on one pre-state only 1 runs; a tampered, deleted or read-only log gives log_failed with the host unchanged; key counter rollback is refused as counter_regressed and logged; corrupt key state is refused.
- Run-6 items: (2) the final compare after the temp fsync is fixed (mutant replace-compare-before-tmp killed). (3) UTF-16 path lengths are fixed (boundary checked). The four run-6 weak tests (run lock, wrong_session logging, created-folder rollback, temp suffix) are now killed. Item (1) is only partly fixed (BROKE 1).
- Criterion saboteurs: skip-validate-in-run KILLED (2 tests fail), skip-post-consume-recheck KILLED (3 tests fail).
- Survivors that are equivalent or fail closed:
  - rel-no-maxname: PATH_LIMIT catches it on this host; with long paths enabled it becomes an OS error after the press, rolled back.
  - current-no-regular: the folder attribute 0x10 is refused by the attribute check.
  - shape-no-kind: the key would show "WRITE host-test 1f read.only", but execute dies with a KeyError in _check_state, so nothing is written. Worth a test, because the kind check is what keeps model-chosen text off the key.
  - Re-derived by the plan compare or the undo hash: shape-no-filekeys, shape-no-sorted, shape-no-dirs-resolve, validate-no-files, unb64-no-validate, unb64-no-size, run-undo-no-shape.
  - state-no-rmdir-isdir: declared in D-022.
  - undo-for-no-copy, undo-registered-no-copy: in-process aliasing, fails closed.
  - begin-no-own, begin-undo-no-own: declared in D-020.
  - apply-no-mkdir-exists: os.mkdir refuses an existing folder anyway.
  - current-no-link-only: _resolve already refuses links.
  - Removing the ':' check makes `_resolve("x:y")` loop forever (a drive-relative path). That is a hang, killed by timeout; the real code refuses ':' first.
- test_gate.py alone does not catch A-no-verify, A-no-class-match or A-no-needs-undo. These guards are owned by P1-UNIT (skip-signature-check saboteur), so they are not counted here.
- Declared crash limits (D-006 LOG_HEAD is later work):
  - A BaseException between consume and the approved append leaves a gap in the logged counters.
  - A BaseException mid-apply leaves a partial write with no rollback and no undo right, though the full undo is in write.approved.
  - A non-OSError after consume (only via the declared junction-swap race onto another drive: commonpath ValueError) escapes without a log record.
- A write followed by its undo strips Zone.Identifier (Mark of the Web). Declared in D-022, and it gives no capability beyond a plain approved write.
- Undo records copy old bytes into the log, and read() needs no token inside the root. Rule 8 (credentials) maps to P2-GUARDRAILS (D-006); the uncommitted D-025 work addresses it.
- The live working tree is not HEAD: DECISIONS.md has +30 lines (D-025) and src/mas_device/gated.py is untracked. That makes P2-GATE STALE there. Only a99fcbc was judged.
- test-results.json and ACCEPTANCE.md in the live tree were rewritten at 10:04:50 by another process. This evaluator ran only verify and status (about 10:00) and changed no tracked file.

REQUIRED BEFORE PASS:
1. Give the rollback restore its own final compare after the temp file is on disk: `_replace_file(p, prev, expect=_sha(written))`, with a Refused treated like OSError so the file is named in left_changed. Add a test that injects the owner's save during the rollback's fsync. Correct D-024 and the gate.py rollback comment.
2. Add a test that kills removing the `_apply` per-file pre-state check for a DELETE: an owner edit during the write.approved append must survive.
3. Add a test that kills removing the rollback restore of an overwritten existing file: a failed write over existing files must leave them at their pre-state.
4. Add a test that kills removing the rollback's re-creation of folders that a failed undo removed.
