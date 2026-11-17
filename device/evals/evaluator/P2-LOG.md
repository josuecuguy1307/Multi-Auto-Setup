# Evaluator report — P2-LOG, run 1 (2026-11-17, built-in Plan agent with `.claude/agents/evaluator.md`, fresh context), verbatim

Evaluated commit: fd02218 (`src/mas_device/log.py`, `device/tests/test_log.py`, 19 tests).

```
UNIT: P2-LOG
VERDICT: FAIL
RAN:
.venv/Scripts/python -m pytest -q -p no:cacheprovider device/tests/test_log.py   (19 passed)
.venv/Scripts/python runs/eval/p2log/attack.py   (about 35 tamper, format and concurrency attacks on the real src/mas_device/log.py)
.venv/Scripts/python runs/eval/p2log/mutate.py   (15 patched copies of log.py, each run against test_log.py; the M0 sanity mutant goes red)
BROKE:
1. Rewrite history and extend it past a known head: changed record 3's payload, re-chained 1..5, added forged record 6.
   verify(path, expected=head_at_5) returns ok=True. _scan compares against the known head only when the file is shorter or the same length.
2. Truncate and forge a longer tail: keep 1..2, append forged 3..6. verify(path, expected=head_at_5) ok=True (same cause).
3. A normal append can permanently break the log: log.append("host", {"name": "vm evil"}) succeeds; json.dumps(ensure_ascii=False)
   writes U+2028, U+2029, U+0085 unescaped and str.splitlines() splits on them. verify then reports "record 2 does not chain" and
   AuditLog.open raises LogBroken forever. Same with type="t ". Any string the model passes into a payload can do this.
4. The logger extends a broken log: a same-size in-place edit (b'"n": 1}' -> b'"n": 7}') then append() wrote record 6. The pre-append check
   compares only the size. The docstring's "re-checks the file's tail ... refuses to extend a broken log" is false.
5. Truncation is forgotten across reopen: cut to 2 records, AuditLog.open(path) + append() continued at seq 3. open() takes no known head;
   nothing stores one or has the key countersign one (LOG_HEAD is only an enum value; no module imports log.py).
6. Two writers race: two AuditLog instances, one append each at the same moment: chain broke in 128 of 200 runs (both write seq 6), log then
   unopenable. Size check and write are not atomic; no file lock. Eight threads sharing one instance stayed fine.
7. A partial last line (crash mid-append) is detected but makes the log permanently unopenable, no recovery path; bad_seq reported as 1, not 6.
8. Minor: records() returns tampered data without verifying it. CRLF line endings or trailing spaces still verify (content unchanged, not byte-stable).
Held: edit of any field of a middle record with or without fixing its hash, deleting a middle line, reorder, duplicate lines, duplicate keys
(top level and nested), trailing blank or garbage lines, BOM, NFD payload, seq as true / 1.0 / "1", float at, extra field, uppercase hash,
bare CR separator, escaped \n in a payload.
WEAK TESTS (mutants that left all 19 tests green):
- M7 dropping the "head hash differs from the known head" check (no same-length rewrite verified against a known head).
- M10 dropping the exact-field-set check (an extra field outside the hash would verify).
- M9 / M11 dropping the bool exclusion and the int checks on seq and at.
- M4 dropping seq == n. M6 dropping the partial-line check (nothing tests a full record with no trailing newline). M14 dropping the domain prefix.
- Missing tests: file longer than the known head, U+2028/U+2029/U+0085, same-size edit then append, reopen with a known head, two writers.
- Killed as intended: prev check (M1), size re-check (M2), dup-key check (M3).
LABELS:
- "Model process cannot write it" is not enforced and not tested; any same-user process can write the file; 0o600 means nothing on Windows.
  The SIMULATED label is honest, but the criterion title states the property as met.
- The docstring describes the gate keeping the head and the LOG_HEAD countersign as existing; they do not.
- "Refuses to extend a broken log" is false (attack 4).
REQUIRED BEFORE PASS:
1. When the file is longer than the known head, require record expected.seq to have hash expected.hash; tests for longer-file and same-length rewrites.
2. Write records as pure ASCII (ensure_ascii=True) and split only on b"\n"; test U+2028, U+2029, U+0085 in payload and type.
3. Before each append re-check the tail's bytes/hash, not only the size; test a same-size in-place edit then append.
4. Let open() take a known head and fail on truncation, or document and test where the head lives between runs; do not describe LOG_HEAD as existing.
5. Make multi-writer appends safe (OS file lock across stat and write) or reject a second writer explicitly; test two instances or processes.
6. Tests that kill M9, M10, M11, M4, M6, M14; decide a recovery path for a crash mid-append and fix the bad_seq it reports.
7. Narrow the criterion title ("model process cannot write it: SIMULATED, not enforced on host") or add a test that the model's tool path has no write route to the log.
```
