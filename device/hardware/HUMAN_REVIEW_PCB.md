# HUMAN_REVIEW_PCB.md — the key board

> **NO FABRICAR sin revisión humana.** Passing ERC, DRC and fabrication checks only proves the files are self-consistent. Nothing here has
> been validated electrically, simulated as a circuit, built, or powered. Label: **DESIGN-ONLY**.

Reviewer: a person with hardware experience, with the STM32U585 datasheet (DS13086), reference manual (RM0456) and every part's datasheet
open. Tick each item, write the date and your name, and note any change made. Source of the list: advisor review A-1 §6 (DECISIONS.md).

| # | Check | Done (date, name) | Notes |
|---|---|---|---|
| 1 | **MCU pins**: every STM32U585 pin used against the datasheet pinout for the exact package and part number. BOOT0 strap, NRST, VCAP/SMPS pins, every VDD/VSS/VDDA/VDDUSB pin. Any pin marked UNVERIFIED in `pinmap.md` checked one by one. | | |
| 2 | **USB-C**: separate 5.1 kΩ pull-downs on CC1 and CC2 (not shared), ESD part at the connector, shield termination, D+/D− routed as a pair and swapped correctly on both receptacle rows. | | |
| 3 | **Power**: LDO dropout at the worst-case VBUS and its thermal dissipation; no more than 10 µF total on VBUS (USB inrush limit); decoupling per supply pin, close to the pin; VDDA filtering. | | |
| 4 | **Button**: wired directly to one MCU GPIO that can be assigned to the secure world; reads "not pressed" if the button or a joint is open; not shared with a boot strap; no other driver on the net. | | |
| 5 | **SWD**: debug access for bring-up (header or pads present), and the plan to lock it (RDP level, TrustZone option bytes) acknowledged before any key is provisioned. | | |
| 6 | **Clock**: crystal-less USB with HSI48 + CRS confirmed for STM32U585 USB full speed, or a crystal with correct load capacitors. | | |
| 7 | **Footprints**: every footprint against the manufacturer's drawing; pin 1 marks; connector mechanics against the board thickness and the enclosure. | | |
| 8 | **OLED module**: pin order of the exact SSD1306 module bought (VCC/GND order varies by vendor), I²C pull-up values, LED currents. | | |
| 9 | **Fab capability**: stackup, trace/space, via and drill sizes against the chosen fab's rules; test points; fiducials if assembled. | | |
| 10 | **BOM**: exact MPNs, lifecycle status, stock on a current date (the snapshot in the repo is dated and goes stale), alternates, DNP marks, licence of any derived design. | | |

Sign-off (only after all ten rows): ______________________ date ________

Until this file is signed, the fabrication outputs in `device/hardware/fab/` must not be sent to any manufacturer.
