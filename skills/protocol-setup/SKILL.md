---
name: protocol-setup
description: Use when the owner has picked an option for an analysed machine and it has to be set up, one step at a time, each step approved by the owner, with a minimal call to prove it answers and an undo for what was done.
---

# Setting up the picked option (a harness run on the coordinator)

Start only for a machine whose analysis is stored AND whose option the owner picked (the coordinator refuses otherwise, with no model call). You never write a plan, a command or a path: code builds the plan from a closed recipe and the machine's measured facts, and code refuses what it cannot do with a typed reason (`unsupported_cell`, `no_build_in_catalog`, `does_not_fit`, `no_disk`, ...). Your part is to choose, one action per turn, what to do next from a small menu.

## Two steps
`prepare`: `load_context` reads the stored analysis and the owner's pick; `recheck_fit` asks the machine for its memory NOW and says whether the pick still fits; `build_plan` makes the plan. A plan that cannot be made ends the run as `cannot_install` with the reason. A plan that is made moves to `run`.
`run`: `run_step N` asks the OWNER to approve step N of this exact plan (they see the exact command, what it writes and how to undo it), and only then does the machine run it; steps run in order. The last step is the minimal call: a short prompt to the local server, its answer and its time recorded as a tested fact. `undo_step N` undoes the latest done step (also approved by the owner); it removes exactly what the plan wrote and stops what it started. `finish_setup` ends the run once it is verified (`done`) or everything done was undone (`rolled_back`).

Nothing runs without the owner's approval of that step, bound to the plan's hash: you cannot approve, and an approval of one plan, step or action is not one of another. The local server listens on 127.0.0.1 only.
