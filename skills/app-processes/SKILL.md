---
name: app-processes
description: Use when you need to know which programs are running on this machine, by executable name, split into system and user programs.
---

# Running programs

Counts the running programs by executable NAME and splits them into system programs (under the operating system's own directories) and user programs. It tells a quiet laptop from a busy server and shows which developer tools are alive.

Only names are read. Command lines and environments are never collected: they carry tokens, paths and other people's data, and the name is enough to recognize a program. The list is capped at the 80 most numerous names.
