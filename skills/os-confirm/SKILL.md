---
name: os-confirm
description: Use when the coordinator must put a native confirmation of the operating system in front of the owner, on top of whatever chat he is using, to approve one exact plan.
---

# A native confirmation, per operating system

The software never shows its own window. When a chat cannot carry the owner's approval (no host confirmation, or a model sits between the chat and the owner), the coordinator asks the operating system to
raise its own dialog with the plan summary and the plan's hash. The answer is the owner's only if a person pressed the button in that dialog: a model can call tools, it cannot press a button in a window it
did not open.

`references/dialogs.json` is the table: for each operating system family, the tools to try in order, the program and its arguments (`{title}`, `{message}` and `{timeout}` are the only placeholders and are
passed as separate arguments, never through a shell), which output means approved, declined or timed out, and an optional stronger step (macOS: LocalAuthentication, which asks for Touch ID or the
account password). A family or a machine with none of these programs has no native confirmation: the answer is `unavailable`, never a guess, and the owner panel is the way.

Windows Hello is not built (no verified way to call it from a plain script here); Windows uses the system message box. Both Windows and Linux are UNTESTED until they run on a real machine.
