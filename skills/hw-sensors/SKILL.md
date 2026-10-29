---
name: hw-sensors
description: Use when the temperature, voltage or fan sensors a Linux kernel exposes should be listed, for example on a small board.
---

# Kernel sensors

Lists the sensor chips the kernel reports through `sensors`, with how many channels each has and of what kind (temperature, voltage, fan, current, power). Only names and channel kinds are kept; the readings are volatile and are not stored here.

If `sensors` is not installed the answer is a typed unknown, not an empty list: an empty list would claim the machine has no sensors.
