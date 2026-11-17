# Evaluator report — P1-UNIT, run 6 (2026-11-17, project `evaluator` agent), summary. Evaluated commit 1670e65. VERDICT: FAIL

- BROKE: nothing in the real code on a model-reachable path (replay also into a fresh interpreter, every tampered field re-signed with the
  real secret, S+L, TTL edges, NaN clock, class/type confusion, 32-thread double spend). 58 of 67 mutants killed; the 4 saboteurs killed.
- WEAK TESTS (fail open): a process-global nonce counter or a module-level seeded RNG survived every test, because the restart test only
  restarted the gate inside one process; across real restarts a spent token would replay.
- NOTES: keymodel.request read each field twice (check, then copy); Gate.begin returned its stored Pending (in-process expiry bypass);
  no test that each class shows its own name on the key.
- Fixed in this session: a test that first nonces from five fresh interpreters are all distinct; the key copies once and checks the copy
  (test with a request whose later reads change); Gate.begin returns a copy of its pending (test that mutating it cannot defeat expiry);
  every class's summary starts with its own name (test).
