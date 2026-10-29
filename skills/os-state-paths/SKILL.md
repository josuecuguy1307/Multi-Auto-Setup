---
name: os-state-paths
description: Use when the folder where this machine keeps the software's own state (keys, pinned peers, the event log) has to be chosen for an operating system.
---

# Where the software keeps its state, per operating system

The code never writes a path of its own. It reads `references/state-dirs.json`, which maps an operating system family (`darwin`, `linux`, `windows`) to a folder template, and expands the template with the machine's own environment. The owner can always override the folder with `--state-dir` or `MAS_STATE_DIR`.

The folder holds private keys, so it must be private to the user who runs the software: that is the per-OS layer's job (`protect_private`), not a path choice. A family not in the table has no default folder: the owner must name one.

Add a family by adding a row; the template may use `~`, `%NAME%` and `${NAME:-default}`.
