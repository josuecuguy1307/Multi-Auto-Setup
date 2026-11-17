# Evaluator report — P1-UNIT, run 3 (2026-11-17, project `evaluator` agent), summary. Evaluated commit 8072e4e. VERDICT: FAIL

- Every run-2 item held against 47 mutants (PYTHONPATH pointed at each copy, mas_device.__file__ checked) and a 4-thread x 300 race on sign().
- BROKE: the key model accepted non-bytes fields of the right length, then spent a counter and crashed in pack_body; a non-numeric gate clock
  reading was stored and wedged every later begin; a clock rollback left unconsumable pendings holding the cap; a token whose len() lies
  raised struct.error.
- WEAK TESTS: the sweep's isfinite guard; the key's press ordering when its clock goes back; clock-under-lock tested only through a proxy.
- Fixed test-first (red 3155dfd, green fccd483): bytes-only key fields checked before any state change and the body built before the counter
  commit; clock readings must be real finite numbers (never stored otherwise); the sweep drops whatever consume would refuse (past TTL or from
  the future); a token's real length is checked after bytes(); a threaded race test for clock-under-lock; a key-clock-rollback press test.
