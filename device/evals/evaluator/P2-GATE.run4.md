# Evaluator report — P2-GATE, run 4 (2026-11-17, project `evaluator` agent), summary. Evaluated commit 1ccb222. VERDICT: FAIL

- Run-3 items verified by 49 mutants (each a copy on its own PYTHONPATH, mas_device.__file__ checked); both oracle saboteurs killed.
- BROKE: after consume, an OSError in the state re-check (a file locked with share mode 0, even from a separate process) escaped with nothing
  logged; a write plus its "exact" undo widened a file's ACL (inherited from the folder) and dropped the hidden attribute; paths through an
  existing file and over-long names planned fine and failed only after the press.
- WEAK TESTS: resolve for mkdirs in _apply; the post-consume mkdir-exists check; the rmdirs type check; an ADS ":" path; undo_for after an
  undo ran; the same-thread refusal mutant hung the suite instead of failing it.
- Fixed in this session (D-022): any post-consume failure is logged as *.refused with its counter and returned as Refused; the undo is
  declared to restore bytes only (no ACL, timestamps or ADS) and files with non-plain attributes are refused; paths through a file and names
  over 200 characters refused at plan time; tests for the mkdir check, ADS paths, undo_for after an undo, and a watchdog so a deadlock fails.
  Declared equivalent: resolve for mkdirs in _apply, the rmdirs type check.
