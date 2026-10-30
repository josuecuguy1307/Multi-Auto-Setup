---
name: protocol-setup-options
description: Use when the owner wants to choose which AI to set up on a machine that has a stored analysis. It reads that analysis, asks the owner closed questions, build the options with code's filters and explain each one.
---

# Choosing which AI to set up (a harness run on the coordinator)

Start only for a machine whose analysis is stored (the coordinator refuses otherwise). You do not probe the machine again: `load_analysis` reads what the analysis found.

Each turn you choose ONE action from the menu. The owner's questions are closed (their ids and options come from `references/questions.json`, never from you). Ask which AI the owner wants (`which_ai`), and which CLI, which API provider or the sensitivity when it matters. Code builds the options with fixed filters and a fixed order (`build_options`); you explain EACH option in plain words (`explain_option`): why it is there and what limits it. You do not rank them. The owner picks.

## One step
`options` has two exits. `done` needs every option explained; `no_options` needs the fact that no option survived the filters. `build_options` and `explain_option` close the step when an exit's evidence is in. Nothing is installed.
