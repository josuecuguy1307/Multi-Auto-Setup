# Evaluator report — P2-GATE, run 1 (2026-11-17, built-in Plan agent with `.claude/agents/evaluator.md`, fresh context), verbatim

Evaluated commit: 01dd7ff (`src/mas_device/gate.py`, `device/tests/test_gate.py`, 44 tests). The oracle's PASS for P2-GATE is therefore
**not** a closed unit: the evaluator's verdict is FAIL until run 2.

```
UNIT: P2-GATE
VERDICT: FAIL
RAN:
.venv/Scripts/python -m pytest device/tests/test_gate.py -q            (44 passed)
runs/eval/p2gate/a1_case.py (case/unicode duplicate paths), a2_object.py (dict subclass, TOCTOU on the action object),
a3_concurrent.py (two approved writes at once), a4_names.py (8.3 alias, COM¹, CONIN$, "nul .txt", <>|"*?, U+3000/U+200B),
a5_links.py (junction before/inside apply, hardlink, base64 variants, extra keys), a6_undo.py (stacked undo, class reuse, replay, undo of delete),
mutate.py (15 mutants), m6_demo.py
BROKE:
1. Guardrail 6, case-insensitive duplicate paths: plan_write({"etc/APP.conf": b"evil\n", "etc/app.conf": b"port=2\n"}) -> press -> execute
   runs; its undo can never run (pre_state_changed), so port=1 is lost. Same with {"N.txt","n.txt"} (undo apply_failed, file stays),
   {"etc/APP.conf": None, "etc/app.conf": b"x"}, {"É.txt","é.txt"}. 8.3 aliases resolve inside root ("LONGDI~1/f.txt" wrote longdirectoryname/f.txt).
2. Writes something other than what was approved, and a forged undo runs: the gate reads the caller's live action object after consume and
   never copies it. A dict subclass whose __getitem__ returns base64("backdoor=1\n") for "content_b64" only when called from _apply:
   host gets backdoor=1, token and hash are for port=2, log "done.post" claims sha(port=2). Same trick on execute_undo. A plain dict mutated by
   another thread between consume and _apply has the same window. The D-003 RPC would close it, but no RPC exists and the gate takes objects.
3. Post-consume pre-state check bypassed by concurrency: _run holds no lock across check and apply. w1 (port 1->2) and w2 (port 1->3), both
   pressed, run in two threads (log clock slowed 0.5 s): both succeed, host ends at port=3 though w2's approved pre-state was port=1; w1's undo
   is refused; w2's undo restores port=1 (silently drops w1).
Held: junction or dir swap between begin and execute (outside_root); extra keys, list order, non-canonical, whitespace or unpadded base64 via a
direct token (refused); write token as undo, undo token as write, replay, re-offer after undo (refused); stacked undos (LIFO correct); undo into
a folder someone else filled (refused); hardlink write (link broken, outside file intact).
WEAK TESTS (mutant survived, 44 passed):
- M1 pre-state re-check after consume removed: nothing changes the host between _validate_write and consume.
- M6 _validate_write in _run removed: an action with a model-written undo, pressed via _approvals.begin, runs and registers the forged undo.
- M5 link check removed, M10 realpath/commonpath removed: each survives alone (the other covers the one link test).
- M13 undo _check_state before consume removed; M14 mkdirs-exist check removed; M11 sorted/unique check removed: covered only by other checks.
- Killed: M2, M3 (log after apply), M4 (undo_mismatch), M7, M8 (rollback), M9, M12, M15.
LABELS: docstring discloses the in-call check-then-write window but not that it allows a root escape (a5 "parent-swapped-in-apply-window" wrote
through a junction outside root), not that concurrent calls are not serialised, nor that the in-process API trusts caller objects. Minor:
read() follows hardlinks out of root; names with <>|"*? raise raw OSError instead of Refused.
REQUIRED BEFORE PASS:
1. Refuse paths (files and mkdirs) that collide under case folding, 8.3 aliasing or file identity; check an existing target's resolved long
   name equals the requested name. Tests for "A.txt"+"a.txt", É/é, a LONGDI~1 alias.
2. Snapshot the action at entry of execute/execute_undo/begin/begin_undo (e.g. json.loads(ap.canonical(action))) and use only the copy; test
   with a dict subclass or a mutate-after-consume hook.
3. Serialise _run (one gate-wide lock from validate through apply and undo registration); two-thread test expecting pre_state_changed.
4. Tests that kill M1, M6, M5, M10 separately.
5. Docstrings: root escape in the apply window, concurrent calls, caller-object trust (or close them).
```
