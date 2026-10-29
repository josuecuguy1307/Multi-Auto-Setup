---
name: hw-power
description: Use when the battery or thermal state of this machine matters, to tell a portable machine from a fixed one or to explain slowness.
---

# Battery and thermal state

A battery tells a laptop from a desktop or a server. The skill reports charge, whether it is charging, cycle count and health on a Mac, and the capacity and status files the kernel exposes on Linux. The thermal state reports `warning` only when the system says it is throttling.

If there is no battery, the answer is a typed `not_applicable` unknown: a desktop has no battery, which is information, not an error.
