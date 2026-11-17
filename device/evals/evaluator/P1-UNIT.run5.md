# Evaluator report — P1-UNIT, run 5 (2026-11-17, project `evaluator` agent), summary. Evaluated commit c8307da. VERDICT: FAIL

- BROKE: nothing in the real code. Every run-4 attack repaired (buffer rewrites, forged handles, odd tokens and sessions, odd clock
  readings, TTL edges, parse edges, signature malleability, counter rollback, replay into a restarted gate, 32-thread spending).
- WEAK TESTS (fail open if the guard went away): a deterministic per-gate nonce survived every suite and would let a spent token replay
  into a restarted gate; `pack_body(..., handle.req)` in keymodel.sign survived and would sign a host-forged handle.
- NOTES: request() returned the key's own handle object; it copied by iterating the request rather than from the checked attributes.
- Fixed in this session: tests that a spent token never replays into a restarted gate, that nonces never repeat across gates, and that a
  forged handle never changes what is signed; the key now copies exactly the checked fields and returns a copy of its handle.
