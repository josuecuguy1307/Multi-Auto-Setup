---
name: app-tools
description: Use when the language runtimes, package managers and developer tools installed on this machine need to be found, with their versions.
---

# Developer tools

`assets/tools.json` is the table of tools worth knowing about (runtimes, package managers, build tools, version control, containers) with the fixed arguments that print each one's version. One `which` finds which of them exist; only those are asked for their version.

Why two rounds: asking all of them for a version would run over a hundred programs, most of which are not installed. The second round runs only the ones `which` found, each with its own fixed arguments and nothing else: the command policy allows a tool only with the arguments the table lists.

A version string is the first version-looking number in what the tool prints. A tool that prints nothing useful is still recorded as present, without a version.
