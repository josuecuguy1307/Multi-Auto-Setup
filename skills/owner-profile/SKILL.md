---
name: owner-profile
description: Use when the harness puts profile questions to the owner, when he wants to see or change his answers, or when he asks to stop using an environment. Says what the profile controls and what you never do with it.
---

# The owner's profile

The profile is data (`references/profile.json`): what counts as confidential for him, whether a task that touches none of it is public, internal or asked each time, his stance on cloud models (never / only when no local model can / allowed), how many times a failed task may move to a costlier route without asking, local first or the best measured one, and what each machine is for (general, private, not for tasks). He may also close a subtype, an environment or an access ("do not use X").

| It controls | How |
|---|---|
| A task's level | The harness derives it from the profile and the categories the text touches; it asks the owner when it cannot tell. Nobody else, you included, gives a level. |
| The router | Closed things and a "never" stance leave candidates out with the reason `closed by the owner`; the cost order and the escalation limit come from it. |
| A cloud chat | It can never create a confidential task, whatever the profile says. |

Nothing starts closed: an environment is available when an access to it is DETECTED on the machine (a signed-in tool, a key, an installed local runtime). Closing is his choice, recorded, and `mas_profile` with `open` undoes it.

You never fill an answer in. The questions and their options come from the harness; put each one to him as printed and relay his answer with `mas_answer`. To change an answer later, use `mas_profile` with his words.
