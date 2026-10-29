---
name: workspace-sizing
description: Use when the largest language model that is safe to run on another device has to be chosen from its memory and disk.
---

# Choosing a model size for a device

A model that does not fit crashes the device or fills its disk, so the choice is made from measured limits and from the human's limits, whichever is stricter. `references/size-table.json` is the table: RAM brackets with the largest parameter count each is safe for, the sizes the catalog knows how to fetch, the quantization, and the per-parameter weight size. The human owns that table; edit it yourself, the model never writes it.

Three limits apply and the smallest wins: what the RAM allows, what the free disk (capped by the target's own `max_disk_gb`) allows, and the parameter cap the human set for this target. The result names which limit bound the choice, so the report can say "limited by RAM" and not just give a number. If nothing fits, the answer is "no model fits" with the limit that stopped it, never a smaller guess.

The RAM figure is the limit the process is actually held to (a container's cgroup limit), not the machine's total, because a container can be held far below what the host has.
