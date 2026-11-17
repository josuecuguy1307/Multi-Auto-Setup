# Evaluator report — P1-UNIT, run 4 (2026-11-17, project `evaluator` agent), summary. Evaluated commit 1ccb222. VERDICT: FAIL

- Every run-3 item verified and killed by a test (48 mutants under their own PYTHONPATH copies; the 4 criteria saboteurs killed).
- BROKE 1 (fails open): KeyModel.request kept references to bytearray fields; a host showed a benign request, waited for the press, rewrote
  its buffers, and got a token for a different action that the gate accepted. The C firmware copies the request (memcpy), the model did not.
- WEAK TESTS: gate nonce length/uniqueness (M42); the 32-byte session check (M37); a 241-char str token (M40). Notes (fail closed, no RPC
  path): a 2**1100 clock reading raised OverflowError; Gate(session="x"*32) accepted.
- Fixed test-first in this session: the key model keeps its own immutable copy of every byte field; nonces tested 16 bytes and distinct with
  no overwrite; session must be 32 bytes of bytes; a str of token length is malformed; huge clock readings are Rejected("expired").
