# Hardware license

The hardware design files of the MAS portable device are licensed under the **CERN Open Hardware Licence Version 2 — Strongly
Reciprocal (CERN-OHL-S-2.0)**, chosen by the project owner, Martín Josué Arcos, on 2026-11-19 (DECISIONS.md D-014).

SPDX-License-Identifier: CERN-OHL-S-2.0

This covers everything under `device/hardware/`: `design.py` and the generators that produce the schematic and the board, the KiCad
files in `device/hardware/kicad/`, the fabrication outputs in `device/hardware/fab/`, the bill of materials, and the enclosure in
`device/hardware/enclosure/`.

What it means in short: anyone may use, study, modify, make and sell products from these files. Anyone who distributes a modified
design, or products made from it, must make their modified source available under this same licence. Read the licence itself for the
exact terms: https://ohwr.org/cern_ohl_s_v2.txt

Source location (to print on any product made from these files, as the licence asks): https://github.com/josuecuguy1307/Multi-Auto-Setup
(branch `device/v0`, folder `device/hardware/`).

The software in this repository stays under the Apache License 2.0 (`LICENSE`, `NOTICE`). Third-party parts used by the hardware build
(KiCad and its libraries) keep their own licenses (`THIRD_PARTY.md`).

The full licence text is not yet copied into this repository: it should be added verbatim from the official source above as
`LICENSE-CERN-OHL-S-2.0.txt` (NEEDS_HUMAN.md item 6).

NO FABRICAR sin revisión humana: the license does not make the design ready to build (`device/hardware/HUMAN_REVIEW_PCB.md`).
