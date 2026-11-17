# Evaluator report — P2-GATE, run 3 (2026-11-17, project `evaluator` agent), summary. Evaluated commit 8072e4e. VERDICT: FAIL

- Held: nested execute/execute_undo from a log hook refused; owner reset after exceptions; no deadlock; junctions in and out of the root;
  a junction swapped in during log.append refused at apply; foreign target; ADS and device names; no NTFS-equal case pair escapes casefold.
  D-020's "_own in begin is defence in depth" claim accepted.
- BROKE: undo_for(bytearray) raised TypeError; a token could be spent with nothing in the log (post-consume refusal); {"q", "q/r"} planned an
  action that can never run (costs a press).
- WEAK TESTS: target check; resolve in _apply; the reparse-point branch of _is_link (the suite made symlinks, never junctions); con/prn/aux/nul
  individually; trailing dot; "NAME .ext" device forms for hosts before Windows 11.
- Fixed test-first (red, then green in this session): bytearray hashes accepted, other types refused; a refused-after-consume approval is
  logged as `write.refused`/`undo.refused` with its counter and reason; a path that is both a file and a folder of the same action refused;
  device names with a trailing space before the extension refused; tests with real NTFS junctions, a junction swapped in after consume,
  a foreign target at begin and at execute, every device name and trailing dot/space.
