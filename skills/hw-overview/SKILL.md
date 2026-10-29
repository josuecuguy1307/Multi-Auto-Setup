---
name: hw-overview
description: Use when the machine model, chip or core layout of this machine is needed.
---

# Machine model, chip and cores

On a Mac the hardware profiler names the machine, its model identifier, the chip and the core layout (performance and efficiency cores). On Linux the CPU description names the chip, and a small board that exposes its model in the device tree (a Raspberry Pi, for example) reports that too.

The core layout is a string in the profiler's own format (`processors total:performance:efficiency`); it is kept as given and marked as such rather than split, so nothing is invented.
