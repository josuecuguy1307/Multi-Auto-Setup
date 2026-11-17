# Evaluator report — P1-UNIT, run 2 (2026-11-17, project `evaluator` agent), summary. Evaluated commit c31da7e. VERDICT: FAIL

- BROKE (new, from the fix): begin() read the clock outside the lock and swept with an older `now`, deleting newer pendings (1135 of 3200
  under contention); the shared 64 cap let a caller who cannot press block a person's UNDO; as_class accepted a foreign IntEnum and an int
  subclass; parse_action(None/123) and Gate(ttl_s=10**400) raised other exceptions; undo kind " " accepted; NaN key ticks signed; the key
  model took cls=True/1.0.
- WEAK TESTS: pin filter (A4), consume lock (A13), clear-pending after sign (A26), signed cls/action_hash compares (A1/A2), counter `<=` (A3),
  key-model dwell and window (A5/A41/A40), key field checks (A29/A35).
- Fixed test-first in 29c97cd: clock read under the lock and sweep by age only; per-class cap; exact classes; non-text actions and huge TTL
  refused; undo kind must match [a-z][a-z0-9_.-]{0,63}; NaN ticks never sign; tests for every surviving mutant.
