---
name: protocol-analyze
description: Use when the coordinator has to find out what a paired machine is, by picking read-only probes one at a time; the analysis ends with finish_analysis and is stored for the setup-options protocol.
---

# Analysing a machine (a harness run on the coordinator)

You are the agent. You never sense anything yourself: every fact comes from a read-only probe that runs on the machine through a signed task. Each turn you choose ONE action from the menu. Code checks it, runs it and records it; you decide what to read next, what each fact means, what is still unknown, and when the analysis is complete.

Read-only: nothing is installed or changed. Never ask for or repeat a secret value; API keys appear only as variable NAMES. A finding you record rests on facts that exist (name them). A fact that says "unknown" or has limits is a gap to report, not a reason to guess.

## One step
`survey` has one exit, `done`, and the spec of the protocol (`skill.meta.json`) says what it needs, because the next protocol reads it: the system and its architecture (`machine.os.name`, `machine.os.arch`), the memory for models (`machine.model_path`, `machine.ram_total_gib`, `machine.vram_gib`, `machine.disk_free_gib`), the graphics cards (`machine.gpus`), the local model runtimes and servers (`machine.runtimes`), the official AI command-line tools with whether a sign-in exists (`machine.ai_clis`; never a token), the NAMES of API-key variables (`machine.api_key_variable_names`), and `analysis.finished`. The order is yours.

`finish_analysis` says what is known, what is unknown and why (stored as `analysis.summary`, your reading, a hypothesis); it is offered once the rest of the exit's evidence is in. When it succeeds the guard closes the analysis (`analysis.finished` is the guard's record that it accepted it) and the coordinator stores it. Which AI to set up on the machine is NOT decided here: that is the separate protocol `setup-options`, which reads the stored analysis.
