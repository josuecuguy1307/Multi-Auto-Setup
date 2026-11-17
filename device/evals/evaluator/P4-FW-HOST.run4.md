# Evaluator report — P4-FW-HOST, run 4 (2026-11-17, project `evaluator` agent), summary. Evaluated commit d4171a9. VERDICT: FAIL

- Run 3 items met except one: the display-before-latch order was killed by no test (the mock's redraw took zero time, so the dwell alone
  refused). A press between the latch clear and the t_req stamp underflowed the age and answered press_window (fail closed, but it
  destroyed the request, unlike D-017 and keymodel). D-017 overclaimed: the dwell narrows the bait-and-switch, it does not close it.
- Fixed: the mock can take time to draw (MAS_DISPLAY_MS) and press at the end of the redraw; tests plus two order saboteurs; a press
  stamped before the request is no_press with the request pending (MAS_PRESS_IN_IRQ_ENABLE test); D-017 reworded. The latch clear and the
  new press-before-stamp check are each equivalent while the other holds: the saboteur removes both.
