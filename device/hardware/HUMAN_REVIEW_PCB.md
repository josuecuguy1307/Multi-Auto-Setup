# HUMAN_REVIEW_PCB.md — the key board

> **NO FABRICAR sin revisión humana.** Passing ERC, DRC and fabrication checks only proves the files are self-consistent. Nothing here has
> been validated electrically, simulated as a circuit, built, or powered. Label: **DESIGN-ONLY**.

Reviewer: a person with hardware experience, with the STM32U585 datasheet (DS13086), reference manual (RM0456) and every part's datasheet
open. Tick each item, write the date and your name, and note any change made. Source of the list: advisor review A-1 §6 (DECISIONS.md).

| # | Check | Done (date, name) | Notes |
|---|---|---|---|
| 1 | **MCU pins**: every STM32U585 pin used against the datasheet pinout for the exact package and part number. BOOT0 strap, NRST, VCAP/SMPS pins, every VDD/VSS/VDDA/VDDUSB pin. Every pin listed under "Unverified" below checked one by one (pin numbers come from KiCad's stock symbol; the pin map is in `design.py`, `MCU_PINS`). | | |
| 2 | **USB-C**: separate 5.1 kΩ pull-downs on CC1 and CC2 (not shared), ESD part at the connector, shield termination, D+/D− routed as a pair and swapped correctly on both receptacle rows. | | |
| 3 | **Power**: LDO dropout at the worst-case VBUS and its thermal dissipation; no more than 10 µF total on VBUS (USB inrush limit); decoupling per supply pin, close to the pin; VDDA filtering. | | |
| 4 | **Button**: wired directly to one MCU GPIO that can be assigned to the secure world; reads "not pressed" if the button or a joint is open; not shared with a boot strap; no other driver on the net. | | |
| 5 | **SWD**: debug access for bring-up (header or pads present), and the plan to lock it (RDP level, TrustZone option bytes) acknowledged before any key is provisioned. | | |
| 6 | **Clock**: crystal-less USB with HSI48 + CRS confirmed for STM32U585 USB full speed, or a crystal with correct load capacitors. | | |
| 7 | **Footprints**: every footprint against the manufacturer's drawing; pin 1 marks; connector mechanics against the board thickness and the enclosure. | | |
| 8 | **OLED module**: pin order of the exact SSD1306 module bought (VCC/GND order varies by vendor), I²C pull-up values, LED currents. | | |
| 9 | **Fab capability**: stackup, trace/space, via and drill sizes against the chosen fab's rules; test points; fiducials if assembled. | | |
| 10 | **BOM**: exact MPNs, lifecycle status, stock on a current date (the snapshot in the repo is dated and goes stale), alternates, DNP marks, licence of any derived design. | | |

## Unverified by the design flow (check each one; they come from `design.py` UNVERIFIED)

- [ ] Every STM32U585CIT6 LQFP48 pin number (taken from KiCad's stock symbol, not checked against DS13086).
- [ ] VCAP on the LDO (non-Q) STM32U585: presence, count and value (AN5373).
- [ ] Crystal-less USB FS with HSI48 + CRS on STM32U585 (advisor: confident, not checked against RM0456).
- [ ] USB-C J1 symbol/footprint names and the GCT USB4105 land pattern.
- [ ] OLED module pin order (GND/VCC order varies by vendor) and whether the module already carries I2C pull-ups.
- [ ] AP2112K-3.3 dropout and thermal at worst-case VBUS; VDDA tied to +3V3 without a ferrite (ADC unused).
- [ ] USB D+/D- routed without impedance control.
- [ ] Pin 36: the stock LQFP48 symbol names it VDD (no VDDUSB pin); check DS13086 whether it is VDDUSB. Here it is tied to +3V3 with its own 100 nF.

## Deviations and custom rules to review

- [ ] Custom DRC rule **"J1 vendor land pattern"** (`kicad/mas_key.kicad_dru`): inside J1 only, hole clearance 0.19 mm instead of the
      board's 0.25 mm, because the stock GCT USB4105 land pattern puts the NPTH pegs 0.194 mm from pads A1/A12/B1/B12. Check against GCT's
      drawing and the fab's NPTH-to-copper capability.
- [ ] Board is 20 × 54 mm, not the 48 mm of the advisor's spec (A-3): the 1×4 OLED socket did not fit otherwise.
- [ ] All tracks are 0.2 mm, power included (load < 50 mA); the spec's 0.4 mm power tracks were not used.
- [ ] Tracks were placed by a small grid router written for this repo (`gen_pcb.py`), not by a person or a commercial router:
      look at every route, especially USB D+/D− (no impedance control, not length-matched) and anything under the USB-C shell.

- [ ] No reference designators on the silkscreen: they are on F.Fab only (assembly drawings need them; silk was kept clear of pads).
- [ ] J1 (USB-C) courtyard sits about 0.5 mm past the board edge on purpose (the receptacle mouth overhangs): check against GCT's drawing
      and the enclosure.
- [ ] Inside J1's courtyard (under the USB-C body and its pad row): the D+ bridge between B6 and A6 and the D− bridge between A7 and B7,
      the VBUS bridge, CC1 (tracks on both layers and two vias, at about (10.45, 6.05) and (9.40, 5.40) mm from the board's top-left),
      CC2, a VBUS via at about (10.90, 8.75) mm, four GND stitching vias at the courtyard's lower edge beside J1's GND pads, and a few
      short GND tracks on F.Cu. Check clearance to the shell and the pegs.
      (Corrected after the board evaluator's run 2, which found the earlier line incomplete.)
- [ ] ATECC608B DNP footprint (D-008) was dropped from v0; add it back only if the review wants the option.
- [ ] J3 (SWD) sits in the middle of the board below the MCU, not on the long edge as A-3 suggested (routing space); check access for a probe.
- [ ] Decoupling: one 100 nF per supply pin (pins 1, 9, 24, 36, 48) within 4 mm of the pin, plus 1 µF on VDDA and 4.7 µF on VCAP (pin 22)
      (fixed after the board evaluator's run 1). USB D+/D- run on the top layer only, with no vias.
- [ ] USB ESD path (fixed after the board evaluator's run 2): each data line runs J1 → U2's connector-side pin → across U2 → U2's
      MCU-side pin → U1, with no other copper from J1 to U1 (tested). U2 is turned 180° so the two lines nest without crossing; D+ is
      about 25.6 mm and D− about 24.4 mm of copper. They are not a coupled pair: they pass U2 on opposite sides (up to about 4 mm
      apart), then run 0.4–2 mm apart to U1; lengths are similar but not tuned. No via sits in any pad (tested). U2's VBUS pin has two vias,
      one under the package between the pad rows (about (9.95, 10.20) mm) and one beside pad 5 (about (7.85, 10.50) mm); its GND via is
      beside pad 2 (about (12.15, 10.50) mm). (Corrected
      after the board evaluator's run 3.)
- [ ] Vias next to pads: about 36 vias sit 0.05–0.1 mm from a pad's edge. The board's solder-mask margin is 0 and vias are tented;
      with a fab's usual mask expansion the via ring may show beside the pad. Check against the fab's mask rules (item 9).
- [ ] Nets that cross under the USB D+/D- tracks on B.Cu: +3V3, I2C_SCL, I2C_SDA, VBUS. A deviation from A-3 ("unbroken
      bottom GND" under D+/D-): the USB pins are on the MCU's right side, so nets beyond them must cross. Acceptable for USB full speed
      (12 Mbit/s) over these short runs; a reviewer may prefer moving the crossings or adding stitching vias beside them.

Sign-off (only after all ten rows): ______________________ date ________

Until this file is signed, the fabrication outputs in `device/hardware/fab/` must not be sent to any manufacturer.
