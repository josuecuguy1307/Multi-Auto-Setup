---
name: harness-chains
description: Use when a task needs the owner's approval to read something from one of his computers, or needs a local model that is not set up or measured yet, and the harness has to carry the request through those steps by itself.
---

# Chains: the harness carries a request through its steps

The chat model is not asked to remember a step. When a task names an input (a folder of one of the owner's computers), the harness walks `references/chains.json` by itself and the model of the chat only relays each question to the owner and each answer back.

## The flow `task_with_inputs`
1. **approve_inputs**: ONE chat question per input, naming the computer and the folder and saying what is read (names, kinds, sizes and dates, never inside a file) and what comes back to the chat. Nothing is read before his yes; his answer is recorded as his.
2. **read_inputs**: the node lists the folder (`list_dir`). The listing is HELD by the harness. The chat is told only a count.
3. **route**: the router decides with the level the owner's profile gives (a folder's names touch his `personal` category, so it is confidential if he said so). A confidential task goes only to a local environment measured here, on the computer he named.
4. If there is **no local candidate**, **propose_setup**: from the stored analysis of that computer the options that fit are built by code (`setup-options`); the owner gets ONE question with a recommended model, a runner-up and "neither", bound to the plan. His pick is recorded and the existing setup plan runs, with its own question showing the whole plan.
5. If a local combination exists but is **not measured**, **measure**: the admission exam for it is proposed to him, as for any measurement.
6. When the route exists, the task is created and the owner approves the exact route as for any task; then the model on that computer analyses it.

## What goes back to the chat
Only the model's answer and the route (computer, environment, model). Every name in the listing is replaced by `[name withheld]` in the answer before the chat sees it, and the listing itself is in no tool result, question or event the chat reads. A chat in the cloud still cannot create a confidential task from its own words: the level comes from what the harness holds, not from the chat.

## Limits, said plainly
No stored analysis of the computer means no setup can be proposed (the chain stops and says to analyse it first). Each step is visited a bounded number of times, so a failing setup or exam ends the chain with the reason instead of repeating.
