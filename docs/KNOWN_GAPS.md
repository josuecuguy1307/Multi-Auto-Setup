# Known gaps (recorded for after the experiment; none of these is built yet)

Each item is a limit of the current release, said plainly. They are not promises.

## Machines and pairing
- After `mas join` pairs, the node service is not installed in the same step: the terminal tells the owner to run `mas node run` by hand. It should offer to install the service right there.
- `Pairing code shown on the coordinator:` prints an empty value on the node. It should say "look at the coordinator's screen".
- Goal: the new machine's terminal only announces itself, and pairing is driven from the chat (the code is shown on the node and typed into a native dialog on the Mac).
- The coordinator is bound to a fixed IP in `coordinator.json`; if the router hands the Mac another address (DHCP) the service stops until the setting is changed.
- If a node disconnects, a server it was running keeps running, and an undo waits for the node to come back.

## What the owner sees
- Live progress as a checklist (step, bytes, percent, time left). It needs byte progress from the node, which is a node change.

## Packaging and tests
- `pytest` is a runtime dependency of the package; it should be a development-only extra.
- `tests/test_tools.py::test_registry_adapter_matches...` reads live hardware (`hw.display_present`) twice and is flaky.
- `config/targets.example.yaml` still carries old fixture addresses; they should be documentation ranges.

## Approvals still on the older flow
Unpair, the undo of a chat connection, a task's route, a board decision, an admission exam, a draft and the steps of a chat connection still ask with an `intent_id` (system dialog, host form or panel), not with a chat question. `approval-policy.json` can move each of them once their flow is converted.

## Models
- The options list orders by size, so on a machine with plenty of memory Llama 3.1 8B is the tenth of about fifty models. The owner can ask the chat for it by name; a profile preference ("prefer this publisher") is not built.

## First-run menu
- The first-run `mas` menu (worktree `../env-recognition-menu`, branch `phase5-menu`) is paused.
