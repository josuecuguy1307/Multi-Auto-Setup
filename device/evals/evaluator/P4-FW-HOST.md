# Evaluator report — P4-FW-HOST, run 1 (2026-11-17, built-in Plan agent with `.claude/agents/evaluator.md`, fresh context), verbatim

Evaluated commit: 5d9a8cb (device/firmware core + host HAL; 296 tests).

```
UNIT: P4-FW-HOST
VERDICT: FAIL
RAN: .venv/Scripts/python device/firmware/build.py ; pytest device/tests/test_firmware_host.py (296 passed)
runs/eval/p4fw/a1.py (press/sign/seq/reboot/opcode/tick), a2.py (hand-made flash: max in page B, both pages full, 2^64-3, UINT64_MAX, 0,
torn half-complement, altered pub + fixed CRC), a3.py (repeated program failures across a page rollover), a4b.py (COBS/frame edges,
159-162 byte encodings, 253-508 runs, overflow then valid frame, split frame, 3000 random frames), a5.py (double press, pw=1 boundary,
counters monotonic across 5 reboots), mut.py (11 mutants), a6.py (counter wrap on the surviving guard mutant)
BROKE:
1. Counter rollback, released counters reused. Page A holds 1..512 (full), page B erased. One boot with MAS_FLASH_FAIL_AFTER=0, 513 rounds of
   REQUEST + press + SIGN (each nv_fault). Commit 1 erases B and sets active=B before the program fails; commits 2..512 use up B's slots
   without writing; commit 513 sees B "full" and erases A, which holds the only valid maximum. After reboot GET_INFO reports 0 and the next
   token carries counter 1 (it was 512). Breaks A-2 §3: mas_nv_commit erases the page holding the maximum without first checking that the
   other page holds a durable larger record.
2. A key-page change after the key has signed is accepted: approve once, set flash[0:8192] to 0xFF, new TRNG file -> new identity, old
   counter log kept, counter 2 signed with the new key. The "never silently change identity once a counter exists" rule covers only k<0
   (corrupt CRC), not k==1 (erased page).
3. Press window aliases at the 2^32 tick wrap: REQUEST, T 0xFFFFFFFF, T 1001, P, SIGN -> ok (~49.7 days later). The gate's 120 s TTL catches
   it on the host, but the key's own window guarantee does not hold. Low severity.
Held: no token without a press after its request; one press one token; pending seq dies on reboot; every other opcode -> unknown_opcode,
flash unchanged; 30000 ms accepted / 30001 refused; images near 2^64 and UINT64_MAX fail closed; torn/garbage/two-page images never lower
the counter; altered pub with fixed CRC -> nv_fault; parser edges clean; 3000 fuzz frames: 0 tokens, flash unchanged.
WEAK TESTS (suite stays green with the guard removed): readback compare in mas_nv_commit (no "program ok but bits differ" mock mode);
non-erased-slot check (hidden by next_slot logic); monotonic/range guard (no test near 2^64; mutant wraps to counter 0); pub-vs-derived-pub
compare in mas_init (no altered key page with valid CRC).
Killed: SIGN seq check, cls/pw range, fault flag, key-regeneration log check, next_slot-after-fail, pending.valid clear, pw cap.
LABELS: mas_core.c says "EMULATED on QEMU/Renode" before any emulator run (say planned); build.py points to a missing
device/firmware/README.md; A-2 asks for a libFuzzer target, ~200 differential scripts and 10k random frames (repo: none, 60, 1500);
PROGRESS says "3 saboteur builds that the suite catches" but two saboteur tests only show the saboteur misbehaves, not that the suite fails.
REQUIRED BEFORE PASS:
1. Never erase the page holding the maximum unless the other page holds a read-back record > cur (or latch a fault after a failed commit);
   test BROKE 1.
2. Erased key page with a non-empty counter log -> fault, not regenerate; test.
3. Make the window robust to tick wrap, or document and test the aliasing as an accepted limit.
4. HAL mock mode where program reports success but bits do not stick; test SIGN -> nv_fault, no token.
5. Tests killing the surviving mutants (pre-programmed next slot, counter near 2^64, altered seed/pub with recomputed CRC).
6. Labels: no EMULATED before P4-FW-EMU; add device/firmware/README.md or drop the reference; meet or amend A-2 sizes and libFuzzer; reword
   the saboteur claim.
```
