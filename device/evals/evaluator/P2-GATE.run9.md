# Evaluator report — P2-GATE, run 9 (2026-11-18, evaluator agent, fresh context). Evaluated commit 1c65ad2. Verbatim.

UNIT: P2-GATE
VERDICT: FAIL
RAN: (commit 1c65ad2; clean copy runs/eval/p2gate9/tree from git archive HEAD; scripts and outputs in runs/eval/p2gate9/)
- git diff 2299003 1c65ad2: gate.py, approval.py and log.py are unchanged since run 8. Only test_gate.py gained 2 tests (+ D-028's gate paragraph)
- PYTHONPATH=<copy>/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python -m pytest -c device/acceptance/pytest.ini --rootdir=<copy> -q -p no:cacheprovider <copy>/device/tests/test_gate.py -> 143 passed (mas_device.__file__ is in the copy, no PYTEST_* vars set)
- scripts/device_accept.py status (P2-GATE PASS 143, 2 saboteurs) and verify (rc 0, no output)
- run 8's attacks re-run against the copy (att/*.py, outputs *.out): a1_tokens 54 held; a2_actions 98 held; a3_paths 36 held + the known ı.txt flag (a separate file, i.txt unchanged); a4_runtime 11 held; a6/a6b path limits identical (259 written, 260 refused, astral counted in UTF-16); a7_misc identical to run 8; b1_rollback t1-t6 identical; b3_none_expect: forward create now pre_state_changed, owner's file kept
- c1_owner_shapes.py (new): owner puts a folder at a create's name, deletes or swaps a file for a folder during an edit, writes a name inside a folder the gate just made, takes the 2nd of two creates, and puts a folder or an empty file at a deleted name during the rollback restore. All held: refused, the owner's object kept, named in left_changed, no temp file left
- c3_dangling.py (new): dangling junction placed at a create's name during the fsync: refused (apply_failed), on the real tree and on mutant replace-lexists-exists
- mutate9.py: run 8's 108 mutants, each in its own copy (import path asserted, 420 s timeout). 87 killed, 21 survived. extra9.py: 6 new mutants on the final compare and rollback. 4 killed, 2 survived
- c2_owner_delete.py run against the real tree and against mutant replace-now-none-skip

BROKE: nothing (on the real code, no path a model or a host-side attacker can reach violated a 4.1 guardrail)

WEAK TESTS:
1. The "target vanished" branch of `_replace_file`'s final compare (expect = a sha, now = None) is untested. Mutant replace-now-none-skip (`if now != expect:` -> `if now != expect and now is not None:`): all 143 tests pass. It fails open on two paths a host-side process reaches with the same timing as run 8's item (c2_owner_delete.py):
   a. Forward edit: plan_write({"etc/app.conf": b"port=2\n"}), pressed. The owner deletes etc/app.conf while the gate's temporary file is being fsynced. Real tree: Refused pre_state_changed, the file stays deleted. Mutant: execute returns OK and app.conf == b"port=2\n". The gate ran an approval bound to pre = sha("port=1\n") against a pre-state that no longer held (rule 3), and it overrode the owner's delete.
   b. Rollback restore of a replaced file: a.conf = b"a=1\n", b.conf = b"b=1\n". plan_write({"a.conf": b"a=2\n", "b.conf": b"b=2\n"}). b.conf's replace fails, and the owner deletes a.conf during the restore's fsync. Real tree: a.conf stays deleted and left_changed == ["a.conf"]. Mutant: a.conf == b"a=1\n" comes back, and left_changed == [], so write.failed says nothing of the owner's change. The owner's delete is reverted without approval (rule 1), and the log record is wrong (rule 5).
   Test 1060 and the rollback-restore fsync test cover "the owner edits". Run 9's two new tests cover "the owner creates" (expect None). Nothing covers "the owner deletes" during the fsync.

LABELS: ok. SIMULATED is still in the gate.py and test docstrings, criteria.yaml and ACCEPTANCE.md. D-028's gate paragraph matches the code and the tests. Run 8's minor wording issues are still there: `_apply`'s docstring says an edit after the last check "is never overwritten" without naming the read-to-os.replace gap that D-024/D-026 declare, and the Refused text says "left changed by someone else" even when the rollback stopped on the gate's own OSError.

NOTES:
- Run 8 REQUIRED item 1 is fixed. Mutant replace-expect-ignored-none is now KILLED by test_a_new_file_never_overwrites_one_the_owner_created_during_the_fsync and test_the_rollback_never_overwrites_a_name_the_owner_recreated_after_a_delete. These narrower variants are killed too: apply-create-expect-any (forward create skips the compare) is killed by the first test, and rollback-restore-deleted-expect-any (restore-of-deleted skips the compare) by the second. I traced both tests: the fsync hook fires only from `_replace_file`, so the log's fsync does not trigger it, and the a.conf rollback goes through the 2nd `_replace_file` call as claimed.
- Nothing regressed. The survivor set is run 8's minus replace-expect-ignored-none. Those 21 keep run 8's classification: re-derived by the plan compare or the undo hash, declared (D-020 begin-no-own / begin-undo-no-own, D-022 state-no-rmdir-isdir), fail-closed in-process aliasing, equivalent (apply-no-mkdir-exists, current-no-link-only, rel-no-maxname, apply-oserror-escape), or P1-UNIT's (A-no-verify, A-no-class-match, A-no-needs-undo). shape-no-kind still survives and fails closed. Run 8 suggested a test for it; none was added.
- New equivalent: replace-lexists-exists (`os.path.lexists` -> `path.exists()`). A dangling junction at the name makes os.replace fail, so it fails closed (c3). A dangling symlink would need symlink privilege.
- Other new mutants killed: replace-compare-before-fsync (4 tests) and rollback-remove-created-no-check (1 test).
- Criterion saboteurs: skip-validate-in-run KILLED (2), skip-post-consume-recheck KILLED (3). The ':' mutants (rel-no-abs-bs-colon, rel-no-colon) hang and were killed by the timeout. The real code refuses ':' first.
- a7 case 4 is unchanged. A ValueError raised by commonpath after consume escapes unlogged, so a spent counter is missing from the log. It is reachable only through the declared junction-swap race (gate docstring "NOT closed") and only on a host with a second drive (this host has only C:). It fails closed: nothing is written.
- I did not re-run the real-timing scripts (a5b, b2_timeline) because gate.py is byte-identical to run 8. Run 8's result stands: consistent with the declared read-to-os.replace gap.
- Undeclared micro-windows from run 8 (forward delete: `_current` -> os.unlink; rollback's removal of a created file: compare -> os.unlink) are unchanged and still unworded in D-023.

REQUIRED BEFORE PASS:
1. Add a test that kills replace-now-none-skip. The owner deletes the target during the gate's temporary-file fsync for (a) a forward edit (pre = a sha), which must be refused as pre_state_changed with the file still absent, and (b) the rollback restore of a replaced file, where the file must stay absent and be named in write.failed's left_changed.
