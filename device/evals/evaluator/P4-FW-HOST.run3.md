# Evaluator report — P4-FW-HOST, run 3 (2026-11-17, project `evaluator` agent, fresh context), summary of the verbatim report

Evaluated commit: e6d3cbd (317 tests). VERDICT: **FAIL**.

- Run 2's REQUIRED items: all met. 273 chains of torn first boots (7 tearing seeds) and cuts during regeneration: every one ends with a
  valid identity, counter 0 and a working sign. A tampered key page after any released token always gives nv_fault. 24 random fault chains
  and 40 NV-level chains: no rollback, no duplicate counter, no commit after a failure, no identity change. NV guards killed by tests.
  D-016's equivalent mutant is equivalent (also `active_after_erase`, not declared).
- BROKE 1: a press that lands while the new summary is still being drawn approves the new request — REQUEST cleared the latch and stamped
  t_req before hal_display (a mock display that presses during the refresh: R2 "WRITE rm -rf host-a" signed). This order was in A-2 §2:
  the spec was wrong.
- BROKE 2: no minimum time between showing a request and accepting a press (bait and switch): REQUEST R1, 4 s, REQUEST R2, press at 0 ms
  → R2 signed. A design gap the spec allowed.
- Not a break (inside D-005's accepted "rollback by reflash"): flash write access after signing can clear every counter record.
- WEAK TESTS: TRNG failure at first boot ignored (mutant signs with an uninitialised seed); mas_nv_write_key result ignored; its read-back
  removed — all survive.
- REQUIRED: clear the latch and stamp t_req after the display returns (code, keymodel.py, A-2), test with a press inside hal_display; add a
  minimum dwell (or accept the risk in writing); tests for TRNG failure and failed/stuck first-boot key writes.
