---
name: connect-recipes
description: Use when the owner wants a chat they already use (Open WebUI, Claude Desktop, a terminal agent) connected to Multi Auto Setup. The recipes say, per host and per operating system, the steps, the exact files and commands, and the undo.
---

# Host connection recipes (data, not code)

`references/recipes.json` lists, for each chat host, the steps that connect it, in order, and for each operating system the files and commands it uses. Every path and flag is quoted from the tool's own documentation (the `sources` of each host); a cell the documentation does not cover refuses with a typed reason instead of guessing. The code that runs a step is generic and never names a host.

A step is shown to the owner with its exact command, what it writes, its size and how to undo it, and runs only after the owner approves that step with a human gesture. Other entries of a settings file are never touched and never shown; the file is backed up first; the undo removes only our entry.
