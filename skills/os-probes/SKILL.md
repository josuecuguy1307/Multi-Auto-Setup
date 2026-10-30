---
name: os-probes
description: Use when a read-only probe has to find installed programs, the GPU, local model runtimes, AI command-line tools or the memory available for models on Windows, macOS or Linux.
---

# Read-only probes, per operating system

Every probe is read-only and returns typed facts with their evidence. What differs between operating systems is DATA in `references/probes.json`, read by the per-OS layer (`envrec_os.probes`): where installed programs are listed (the Windows uninstall registry keys, the macOS Applications folders, `dpkg` or `rpm` on Linux), which fixed read-only commands report the GPU, which local model runtimes and AI command-line tools exist (names, binaries, ports, the files whose EXISTENCE means "set up"), and which environment variable NAMES look like a provider's key. Code never carries one of these names of its own.

What a probe never does: run anything that changes the machine, read a token, a key or an MCP argument (a login is judged by a file's existence, never its content), contact anything but the machine's own loopback address, or print an environment variable's value.

Every command is a fixed argument list from the table, run without a shell, with a time limit and an output cap, and only if its program is named in the table's allowed list. Add an operating system or a runtime by adding rows.
