# Evaluator report — P2-GATE, run 6 (2026-11-17, project `evaluator` agent), summary. Evaluated commit 1670e65. VERDICT: FAIL

- Held: the run-5 lost-update attacks (0 lost in 185 separate-process runs); rejected tokens logged with authentic counters; doomed paths
  refused before the press; 76 field-tampering mutations refused before consume; 8 threads on one token, one success.
- BROKE: (1) the rollback of a failed multi-file write overwrote or deleted files the owner had saved meanwhile (8-11 of 40-80 runs with a
  separate "save all" process); (2) the final pre-state compare ran before the temporary file was written and fsynced (window measured at
  ~1.8 ms, not "microseconds"); an edit during that fsync was overwritten; (3) the path limit was counted in code points, not UTF-16 units.
- WEAK TESTS: the run lock (two writes released into _apply together both ran); wrong_session logging; rollback of created folders; the
  temporary-suffix length (the test derived its expectation from the constant itself).
- LABELS: D-023's "microseconds", "never overwritten" and its unknown_nonce/mismatch logging claim were inaccurate.
- Fixed in this session (D-024): rollback restores or deletes a file only if it still holds what the gate wrote, otherwise leaves it and
  names it in write.failed (left_changed); the final compare runs after the temporary file is on disk, right before os.replace; path
  lengths measured as the host counts them; tests for all four weak spots (a hard-coded MAX_PATH boundary, a barrier that releases two
  writers into the write together, a wrong_session token, a failed write that created folders); D-023 corrected.
