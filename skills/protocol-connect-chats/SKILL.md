---
name: protocol-connect-chats
description: Use when the owner asks to connect a chat host to Multi Auto Setup. Code finds which hosts are on this computer, the owner chooses, code builds the plan from the recipes, and each step is approved by the owner.
---

# Connecting a chat host (a harness run on the coordinator)

You never write a command, a path or a settings entry: code builds the plan from the closed recipes, and refuses with a typed reason what it cannot do safely (`app_running`, `config_not_plain_json`, `os_not_documented`, ...). Your part is to choose the next action from the menu.

## Two steps
`prepare`: `detect_hosts` looks at this computer (read-only) and records which chats are there; `ask_hosts` puts one closed question to the owner, "Which chats do you want to use Multi Auto Setup in?", with only the hosts that were found. When the owner already named the chat in their request, that request is their answer: `detect_hosts` records it too and `ask_hosts` is not on the menu; `build_connect_plan` makes the plan. A plan that cannot be made ends the run as `nothing_to_connect`, with the reason.
`run`: `run_step N` asks the OWNER to approve step N of this exact plan and only then runs it; steps run in order, the last one reads the result back. `undo_step N` undoes the latest step that is done, also approved by the owner. `finish_connect` ends the run once the connection is verified (`done`) or everything done was undone (`rolled_back`).

Nothing runs without the owner's approval of that step, bound to the plan's hash: you cannot approve.
