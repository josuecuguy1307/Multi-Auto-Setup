# Evaluator report — P2-GATE, run 8 (2026-11-18, evaluator agent, fresh context). Evaluated commit 2299003. Verbatim.

UNIT: P2-GATE
VERDICT: FAIL
RAN: (commit 2299003; clean copy runs/eval/p2gate8/tree from git archive HEAD; scripts and outputs in runs/eval/p2gate8/)
- PYTHONPATH=<copy>/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python -m pytest -c device/acceptance/pytest.ini --rootdir=<copy> -q -p no:cacheprovider <copy>/device/tests/test_gate.py -> 141 passed (mas_device.__file__ is in the copy)
- scripts/device_accept.py status / verify (read-only): P2-GATE PASS 141, 2 saboteurs. verify reports P1-UNIT STALE in the live tree (not this unit)
- re-ran run 7's attacks against HEAD: a1_tokens (54 held), a2_actions (98 held), a3_paths (37; the one "suspicious" is ı.txt, a separate file; i.txt unchanged), a4_runtime (11 held), a6/a6b path limits (259 written, 260 refused, astral characters counted in UTF-16), a7_misc
- b1_rollback.py: 6 deterministic rollback attacks (a deleted file restored; the owner creates a deleted name during the restore's fsync; a forward refusal on the 2nd file; the owner recreates a folder during a failed undo; the owner edits during a failed undo's restore fsync, and the undo right is kept; the owner fills a created folder)
- a5b_rollback.py 80 (owner in a separate process, real timing) and b2_timeline.py 200 (same, with timestamps)
- mutate8.py: 108 mutants, each in its own copy (import path asserted, 420 s timeout), test_gate.py run hermetically. 86 killed, 22 survived
- b3_none_expect.py and b1m.py run against the real tree and against the replace-expect-ignored-none mutant

BROKE: nothing (on the real code, no path a model or a host-side attacker can reach violated a 4.1 guardrail)

WEAK TESTS:
1. The "must not exist" half of `_replace_file`'s final compare (expect=None) is untested. Mutant replace-expect-ignored-none (`if expect is not _ANY:` -> `if expect is not _ANY and expect is not None:`): all 141 tests pass. It fails open on two paths:
   a. Forward create: plan_write({"etc/new.conf": b"gate\n"}), pressed. The owner creates etc/new.conf = b"owner-created\n" while the gate's temporary file is being fsynced. Real tree: pre_state_changed, the owner's file is kept. Mutant: execute returns OK and new.conf == b"gate\n". The owner's file is lost (rule 1), and the registered undo would then delete the name.
   b. Rollback restore of a file the write deleted: a.conf = b"a=1\n", b.conf = b"b=1\n". plan_write({"a.conf": None, "b.conf": b"b=2\n"}). b.conf's replace fails, and the owner creates a.conf = b"owner\n" during the rollback restore's fsync. Real tree: a.conf == b"owner\n" and left_changed == ["a.conf"]. Mutant: a.conf == b"a=1\n", so the owner's file is overwritten.
   Tests 1060 and 1138 only cover expect = a sha (an existing file). Nothing injects an owner write into a create or into a restore-of-deleted.

LABELS: ok. SIMULATED appears in the gate.py and test docstrings, criteria.yaml and ACCEPTANCE.md. D-026 corrects D-024 and declares the remaining read-to-os.replace gap. The rollback comment in gate.py is now accurate. Minor wording: `_apply`'s docstring still says an edit after the last check "is never overwritten" without naming the declared gap that D-024/D-026 name. Also, the Refused message says "left changed by someone else" even when the rollback stopped on the gate's own OSError (e.g. disk full).

NOTES:
- Run 7 REQUIRED items, all verified fixed:
  (1) The rollback restore uses `_replace_file(p, prev, expect=_sha(written))`, and a Refused is caught like an OSError. Mutants rollback-restore-no-expect, rollback-catch-oserror-only and rollback-error-not-in-left are KILLED. Part A of a5b: the owner's save is kept and named.
  (2) The delete pre-state check is covered: apply-no-file-pre KILLED by test_an_approved_delete_never_removes_an_edit_made_during_the_log_append.
  (3) The rollback of replaced files is covered: rollback-no-restore KILLED (3 tests), rollback-restore-expect-prev KILLED.
  (4) The rollback remaking removed folders is covered: rollback-no-remake-rmdir, apply-rmdir-no-record and rollback-not-reversed KILLED.
  D-026's gate claims hold. The log half of D-026 belongs to P2-LOG and was not judged here.
- Real timing, owner in a separate process: a5b gave 1 lost save in 80 runs (run 7: 3/80). b2_timeline gave 0 lost in the 60 runs where the owner actually raced (52 left alone and named, 8 saved after the rollback; 140 runs timed out under load before the owner saw the gate's content). On Windows, a save whose handle is open blocks os.replace, so a loss needs the owner's whole open-write-fsync-close to fit between the gate's last read and os.replace. That is the gap D-024/D-026 declare. The 1/80 could not be timed, so it is consistent with that declared gap, not shown to fall outside it.
- Criterion saboteurs: skip-validate-in-run KILLED (2), skip-post-consume-recheck KILLED (3).
- The 20 other survivors are the same set run 7 classified: re-derived by the plan compare or the undo hash, declared (D-020 begin-no-own / begin-undo-no-own, D-022 state-no-rmdir-isdir), fail-closed in-process aliasing, or equivalent (apply-no-mkdir-exists: os.mkdir refuses anyway; current-no-link-only: _resolve refuses links; rel-no-maxname: PATH_LIMIT). A-no-verify, A-no-class-match and A-no-needs-undo belong to P1-UNIT. New equivalent: apply-oserror-escape (_apply turns every OSError into Refused before it leaves). shape-no-kind still survives: execute dies on a KeyError (fails closed), but the kind check is what keeps model-chosen text off the key display, so a test is still worth adding.
- Mutants that remove the ':' check hang in _resolve (drive-relative path). They were killed by the timeout; the real code refuses ':' first.
- Undeclared micro-windows of the same class as the declared gap: forward delete (`_current` read -> os.unlink) and the rollback's removal of a created file (compare -> os.unlink). Neither has an fsync in it. D-023 words the forward gap only in terms of os.replace.
- t4: when a failed undo's rollback cannot remake n/a because the owner made it meanwhile, x.txt is put back inside the owner's folder (its pre-undo place) and n/a is named in left_changed. Nothing of the owner's is lost.
- Declared crash limits (BaseException between consume and log, or mid-apply) are unchanged from run 7.

REQUIRED BEFORE PASS:
1. Add a test that kills replace-expect-ignored-none. The owner creates the target name during the gate's temporary-file fsync for (a) a forward create (pre=None) and (b) the rollback restore of a file the write deleted. The owner's file must survive, (a) must be refused as pre_state_changed, and (b) must name the file in left_changed.
