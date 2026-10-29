---
name: hw-buses
description: Use when the Thunderbolt ports or PCI devices of this machine need to be counted and listed.
---

# Thunderbolt ports and PCI devices

On a Mac it reports how many Thunderbolt ports exist and which have something connected. On Linux it lists the PCI devices with class, name and the vendor and product ids. It is read-only inventory.

A Thunderbolt port with nothing in it is not an entity; the connected ones are, because a docked device is the first sign of attached hardware.
