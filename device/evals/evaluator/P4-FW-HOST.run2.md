# Evaluator report — P4-FW-HOST, run 2 (2026-11-17, project `evaluator` agent, fresh context), summary of the verbatim report

Evaluated commit: 6bad843 (308 tests). VERDICT: **FAIL**.

- Run 1's BROKE 1–3 replayed: fixed. 37 random chains of cuts (erase modes 0/1/2), FAIL_AFTER, STUCK and reboots (rollover both ways):
  the counter never fell below a released token; neither fault latch clears without reboot; no new identity once the log is non-empty;
  counters near 2^64 and ticks past 2^32 fail closed.
- BROKE 1 (availability, fails closed): a power cut during first-boot key generation can brick the key for good. TRNG seed
  0661851bb1e31131145110929909f260541ea7949e01da850fd862b1581bf375, MAS_CUT_AFTER=9: 9 seed bytes written, CRC bytes still FF FF and
  crc16(page[0:64]) happens to be 0xFFFF, so the CRC passes; derived pub ≠ stored pub → nv_fault forever although nothing was ever signed.
  About 1 in 1000 seeds has such a tear point in bytes 1..63. The existing sweep passed only because it used SEED=range(32), every 3rd byte.
- WEAK TESTS (survive): mas_nv_init max selection replaced by "last valid record" (no test with the maximum in page A while page B holds
  records, no cut at the B→A rollover); non-erased-slot check; the NV-level `failed` latch (hidden by the core latch); `active` set right
  after the erase; erase verify (no erase-fault mock mode); `(any && c <= cur)`.
- Minor: at a saturated counter REQUEST answers ok and shows "#0"; SIGN then gives nv_fault.
- REQUIRED: test max selection B→A (A slot0=1025, B=513..1024 → boot 1025, next 1026) and a cut sweep at the B→A rollover; an NV-level
  harness plus an erase-fault mode that kills the latch, active-before-readback, erase verify, c<=cur and non-erased-slot mutants (or
  remove those guards and their claims); a torn first-boot key write must not lock the key (test that seed with cut 9, sweep every byte);
  REQUEST at a saturated counter → nv_fault.

Attack scripts and mutants: runs/eval/p4fw2/ (git-ignored).
