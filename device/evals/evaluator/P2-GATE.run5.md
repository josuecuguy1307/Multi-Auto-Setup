# Evaluator report — P2-GATE, run 5 (2026-11-17, project `evaluator` agent), summary. Evaluated commit c8307da. VERDICT: FAIL

- Held: every run-4 item (locked files during consume for write and undo, hidden files, denied listings, swapped folders, the locker race
  with 0 missing counters, hidden/through-a-file/long/ADS paths, undo_for after an undo, the nested-write watchdog); both saboteurs killed.
- BROKE 1 (real, no adversary needed): lost update. The last pre-state check ran before the fsync'd audit-log append; an edit landing in
  that window (an editor's autosave) was overwritten in 50 of 60 runs and the logged undo could not bring it back.
- BROKE 2: paths that could never be written still planned and burned a press: MAX_PATH (long paths off by default), names counted in code
  points instead of UTF-16 units, an 8.3 alias inside one action, an undo whose parent folder had been removed.
- WEAK TESTS: other non-plain attributes (read-only, system, temporary), the undo pre-check before consume, nothing-to-delete, an existing
  file over 1 MiB, a failing .refused append, mkdirs resolved in _apply (D-022's "equivalent" claim withdrawn: through a swapped junction the
  mutant creates folders outside the root before the rollback removes them).
- LABELS: D-022's reason for the expired-token gap was wrong (the signature is verified first, the counter is authentic); counter_regressed
  gaps undeclared; "plan time refuses what could never be written" overclaimed.
- Fixed in this session (D-023): each file is compared with its approved pre-state immediately before it is replaced or deleted, and each
  folder must still be absent immediately before it is made; names measured in UTF-16 units and UTF-8 bytes; the host's path limit (259,
  or 32000 with long paths, or 4095 on POSIX) checked with the temporary-name suffix; names that look like 8.3 aliases refused; an undo
  whose folder is gone refused before the press; authenticated tokens refused as expired, counter_regressed or wrong_session are logged as
  *.rejected with their counter; tests kill every listed mutant.
