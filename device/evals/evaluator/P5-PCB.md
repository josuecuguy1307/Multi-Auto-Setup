# Evaluator report — P5-PCB, run 1 (2026-11-17, project `evaluator` agent, fresh context), summary of the verbatim report

Evaluated commit: d4171a9 (20 tests). VERDICT: **FAIL**.

- Held: ERC 0 / DRC 0 / parity 0 even with KiCad's default-ignored checks raised to error; without the custom rule only the 4 disclosed J1
  hole-clearance items appear (0.1944 mm); regeneration byte-identical; every track and via of each of the 19 nets deleted is caught;
  moved, deleted, DNP'd, re-valued (PCB only) and re-footprinted parts are caught; in 157 single-item deletions, each one DRC let through
  was redundant copper. Labels, stamps, _NO_FABRICAR suffixes, no zip, honest BOM: ok.
- BROKE 1: the B.SilkS stamp moved 300 mm off the board, cut to "NO FABRICAR", or hidden still passes the regex test; the silk Gerbers are
  "stamped" only by the inserted G04 comment.
- BROKE 2: decoupling. U1 pin 24 (VDD) is 12.7 mm of copper from its nearest cap (1 µF) and 19.7 mm from the nearest 100 nF; pins 1 and 48
  share C4. A-3 asks for 5 × 100 nF + 1 µF; design has 4 × 100 nF + 1 µF. supply_check passes by counting the LDO's output caps. Not disclosed.
- BROKE 3: USB D− changes layer through two vias; B.Cu VBUS, +3V3 and I2C_SCL tracks run under D+/D−. A-3 says top only over unbroken GND.
  Not disclosed.
- BROKE 4: netlist mutations the suite misses: D+/D− swapped at U1 or J1; U2.3 off USB_DM; PB6/PB7 swapped; BTN on PH3 and BOOT0 on PA0;
  OSC nets on PH0/PH1; 100 nF caps changed to 1 pF, VCAP to 1 µF, pull-ups to 100k.
- WEAK TESTS: stamp regex; fab stamp check finds only the comment; supply_check (counts LDO caps, ignores values, distance, takes pins
  from design.py); USB check by refs only; no-crystal check compares two constants; custom-rule check by name only (a widened rule passes);
  human-review check does not require the deviations.
- LABELS: HUMAN_REVIEW row 1 cites a nonexistent `pinmap.md`. Undisclosed deviations: 4 × 100 nF and none near pin 24; D− vias over cut
  GND; J3 in the middle not on the long edge; ATECC608B DNP footprint dropped. Reviewer must also see: no reference designators on silk;
  J1 courtyard 0.51 mm past the edge; CC1/CC2/D+ tracks and two CC2 vias under the USB-C body.
- REQUIRED: per-pin decoupling (≥ 100 nF within ~3 mm of copper of every VDD/VBAT/VDDA pin) or a recorded deviation, and a per-pin test
  from the netlist; D± on F.Cu only with no B.Cu under them (or a deviation) plus a pcbnew test; silk stamp checked with pcbnew (full text,
  both sides, visible, inside the outline, ≥ 0.8 mm); pin-level netlist tests (PA11/PA12, J1 A6/B6/A7/B7, U2 pins, PB6/PB7, BTN on PA0,
  PH0/PH1/PC14/PC15 unconnected, values from the netlist); .kicad_dru content test; HUMAN_REVIEW fixed and complete, test requires the
  deviations; committed fab/ equals a fresh build ignoring dates.

Attack scripts: runs/eval/p5/ (git-ignored).
