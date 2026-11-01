---
name: secret-entry
description: Use when an access needs a secret the owner must type (an API key), when adding or removing an access, or when a route says that access is missing a variable. Explains how the key is entered without the chat or the model ever seeing it.
---

# Secret entry (a key goes from the owner's keyboard to the node's credential store)

The owner types a key into a **native hidden-field window of the operating system** on the computer he is sitting at. It never goes through a chat, a tool argument, a card or a model. The coordinator reads it from that window, seals it into one write task to the node over the paired channel, and the node keeps it in **its own credential store** (Windows Credential Manager, macOS Keychain, Linux Secret Service). There is no other place: with no real store the entry is refused with `credential_store_missing`, and nothing is written to a file.

What you can do, and what you will see:

| Goal | Tool | What comes back |
|---|---|---|
| Add an access (a key) on a machine | `mas_add_access` | the plan as text; the harness itself opens the approval dialog (Touch ID) on the owner's screen and, right after he approves, the hidden-field window for the key. You call nothing; follow with `mas_job`. If no dialog could be opened, `mas_job` gives the `intent_id` for `mas_request_approval` |
| Remove an access | `mas_remove_access` | the plan (what is set aside, how to undo it); the owner approves in the system dialog; with `undo: true` it brings the key back |
| See whether a route is missing a key | `mas_explain_route` | `access missing: NAME` for the machine; the NAME of the variable, never a value |

Rules you follow:
- You never ask the owner to paste a key into the chat. If he does, tell him not to, and that the window is the only way.
- You never see a value. The only thing you learn is "access present" or "access missing: NAME".
- Removing an access moves the key aside inside the store, so it can be undone without the key passing through anyone. Approve and undo both need the owner's own gesture in the system dialog; a tool call cannot give it.
- A machine whose store is missing (for example a Linux computer with no Secret Service running) cannot hold a key: say so and stop; do not suggest a file or an environment variable as a workaround for a key the harness should hold.

`references/prompts.json` has the window for each operating system as data. A new operating system or window program is a new row there, with no code change.
