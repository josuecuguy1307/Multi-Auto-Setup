# Evaluator report — P1-UNIT, run 1 (2026-11-17, built-in Plan agent with `.claude/agents/evaluator.md`, fresh context), verbatim

Evaluated commit: fd02218 (`src/mas_device/approval.py`, `src/mas_device/keymodel.py`, `device/tests/test_approval.py`).

```
UNIT: P1-UNIT
VERDICT: FAIL
RAN:
.venv/Scripts/python -m pytest -q -p no:cacheprovider device/tests/test_approval.py device/tests/test_unit_cycle.py   (54 passed, 1 failed: no report yet)
.venv/Scripts/python runs/eval/p1u/attack.py   (replay, every 1-byte substitution of all 241 bytes x 255 values, TTL edges, clock rollback,
  NaN TTL, cross-session, other gate instance, class confusion, type confusion, unicode, dup keys, deep nesting, key-state tampering,
  counter overflow, pending growth)
.venv/Scripts/python runs/eval/p1u/mutate.py   (29 mutants under runs/eval/p1u/mut/)
BROKE:
1. Malformed input escapes as non-Rejected exceptions: parse_action('{"x":"\ud800"}') and consume(tok, dict(ACTION, x=chr(0xD800)))
   -> UnicodeEncodeError; parse_action("["*200000 + "]"*200000) -> RecursionError.
2. Expiry fails open: gate clock moved back 1e6 s after begin -> consume accepted. Gate(..., ttl_s=float("nan")) -> nothing ever expires.
   (With time.monotonic this needs configuration or injection, but nothing guards it.)
3. The class check is optional: a LOG_HEAD token for a write action is accepted by consume(tok, action) with cls=None. consume(cls=99)
   raises ValueError, not Rejected. begin(ACTION, True) and begin(ACTION, 1.0) both become WRITE.
4. WRITE with an empty undo is accepted: begin(dict(ACTION, undo={}), Class.WRITE).
5. _pending is never purged: 10,000 begins after expiry stay in memory.
6. Key model (SIMULATED): KeyModel.load accepts {"counter": true}, "-5", 1.9 through int(); counter 2**64-1 commits 2**64 then pack fails
   (key bricked); key tick moved back 1e6 s before the press still signs; Button._Button__last can be set from software.
Held: replay; every byte substitution (0 accepted, 0 non-Rejected); exactly at TTL; restart; cross-session; token for A used for B;
True vs 1; NFC/NFD and escaped duplicate keys; floats, NaN, 1e400, big ints; forged signature; concurrent double spend.
WEAK TESTS (mutant survived, 53/53 green): no_session_check (test accepts "unknown_nonce"), no_undo_hash_check, no_press_window_field_check,
no_domain_check, no_pin_kid_filter, nonce_lookup_any, no_lock, key_no_press_order / key_no_take_on_request / key_no_clear_pending (redundant pairs).
KILLED: no_sig_check, no_nonce_delete_on_success, no_ttl_check, ttl_gt_not_ge, expiry_does_not_burn, no_class_check, no_action_hash_check,
no_action_hash_vs_given, no_summary_check, no_counter_check, no_nfc_check, no_dup_key_check, no_int_limit, no_action_domain, no_undo_required,
key_no_press_window, key_save_after_release, key_no_summary_printable, key_no_stale_check.
LABELS: Mostly ok. Button docstring "nothing can set it pressed from software" is false for the Python model; say it is a convention in the
model and enforced only in firmware.
REQUIRED BEFORE PASS:
1. Map every canonicalising/parsing error (UnicodeEncodeError, RecursionError, ValueError) to Rejected("malformed"); tests for lone surrogates and deep nesting.
2. Expiry fails closed: reject now < created or non-finite elapsed; Gate.__init__ requires finite 0 < ttl_s <= MAX_TTL_S; tests for rollback and nan.
3. cls required in consume; reject bool/non-int classes; Rejected (not ValueError) for unknown values; tests.
4. Refuse an empty or trivial undo for NEEDS_UNDO classes; test.
5. Tests where the key signs a request with one tampered field: foreign session_id, wrong undo_hash, wrong press_window_s, foreign domain;
   N1 token cannot consume pending N2; pinned key_id mismatch; a concurrency test that fails without the lock. Each mutant above must go red.
6. Bound _pending: sweep expired entries and cap; test.
7. Key model: state counter int, not bool, 0 <= c < 2**64-1; refuse before committing an overflowing counter; fail closed when press time <
   request time; fix the Button docstring.
8. Run the evaluator again from a fresh context.
```
