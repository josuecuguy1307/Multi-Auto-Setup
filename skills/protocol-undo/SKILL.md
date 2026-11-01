---
name: protocol-undo
description: Use when the owner asks to undo what was set up on a machine, later and in a new run. It undoes only what the recorded setup plan wrote, one step at a time, each step approved by the owner.
---

# Undoing a finished setup (a harness run on the coordinator)

Start only for a machine whose setup left a record on the coordinator (the plan that was run and which of its steps are done); the coordinator refuses otherwise, with no model call. You never write a plan, a command or a path: code reads the recorded plan and offers only the steps that are done, with the undo that plan itself wrote. Your part is to choose the next action from the menu.

## Two steps
`prepare`: `load_setup` reads the record and makes the undo plan (the done steps, newest first). A machine with nothing to undo ends the run as `nothing_to_undo`. A plan that is made moves to `run`.
`run`: `undo_step N` asks the OWNER to approve the undo of step N (the latest step that is done); only then does the machine run it, and it removes exactly what that plan wrote and reports anything left. `finish_undo` ends the run once everything is undone (`rolled_back`).

Nothing runs without the owner's approval of that step, bound to the recorded plan's hash: you cannot approve, and an approval of one plan, step or action is not one of another.
