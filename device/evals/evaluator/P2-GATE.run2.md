# Evaluator report — P2-GATE, run 2 (2026-11-17, project `evaluator` agent), summary. Evaluated commit c31da7e. VERDICT: FAIL

- Held: every case/Unicode/8.3 attack of run 1; hard links added at any point up to apply; caller-object mutation; write and undo at once.
- BROKE: the RLock let a nested call from a log hook run a second write (both succeeded, the second overwritten); lone-surrogate paths and
  undo_for(non-bytes) raised other exceptions. Labels: COM0/LPT0/COM¹²³/LPT¹²³/CONIN$/CONOUT$ missing from the device-name list.
- WEAK TESTS: realpath check (N8), _own in begin/begin_undo (N5/N6), RLock→Lock (N17). Note: run 1's mutants used -o pythonpath, which does
  not override the editable install, so its kill/survive table was probably measured against unmodified code.
- Fixed in this session: plain Lock plus a same-thread refusal; realpath check tested alone (link check patched out); device names
  completed; surrogate paths and non-bytes hashes refused. Declared (D-020): _own in begin/begin_undo is defence in depth (consume
  recomputes from its own copy), not separately tested.
