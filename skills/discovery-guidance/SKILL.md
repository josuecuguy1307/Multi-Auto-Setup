---
name: discovery-guidance
description: Use when nothing answered a discovery query and the owner needs plain, operating-system specific guidance on what to check, without the software changing anything itself.
---

# What to tell the owner when discovery finds nothing

The software never changes a firewall, a network profile or a router setting. When a query gets no answer it says what is most likely and what the OWNER can do, in the owner's operating system's words. The text is data in `references/guidance.json`: per error code, a common part and a part per operating system family (`darwin`, `linux`, `windows`), for the machine that asked (`node`) and the machine that should answer (`coordinator`). Add a family or a code by adding a row.
