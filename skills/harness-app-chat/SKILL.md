---
name: harness-app-chat
description: Use when you are the chat (Claude Desktop, a terminal agent) the owner talks to and they ask to add a new computer to their network, to see who is waiting to join, or to remove a computer. It says which Multi Auto Setup tool to call, in what order, and what only the owner can do.
---

# The owner's network from a chat: pairing and unpairing

You relay. The harness does the work and the owner decides. Nothing on this page can be completed by a tool call of yours: every step that trusts or stops trusting a computer ends in a system dialog that only the owner can answer.

## Add a computer ("pair a new machine")

1. **See who is waiting.** Call `mas_pair` with no arguments. A computer that ran `mas join` appears in the list with its name, system and architecture. If nobody is listed, say so and tell the owner what to do on the new computer: install Multi Auto Setup (install.sh or install.ps1) and run `mas join`. Do not invent a machine.
2. **The owner picks which one.** If more than one waits, ask the owner which. Never pick for them.
3. **The code appears on the owner's screen, not here.** Call `mas_pair` with `machine` set to that computer's name. A dialog opens on the owner's computer with the 6-digit code; the reply only says it is on their screen. You do not know the code and must never ask the owner to type it to you. Tell the owner to read it from that dialog and type it on THAT computer, at its own prompt. It is valid for about two minutes and one try. If the reply says no dialog could be opened, tell the owner to read the code on the owner screen or run `mas pair` in a terminal.
4. **Wait for the owner to type it.** Call `mas_pair` with `wait_s` set to 15 (no `machine`) until the state changes. If the reply says the code expired, offer to make a new one (step 3).
5. **Approval.** When the computer has typed the right code the HARNESS opens a system dialog on the Mac by itself: the owner reads the computer's name, system and key fingerprint and answers there. You call nothing for it. Call `mas_pair` (no `machine`) to hear the outcome; while the dialog is open the reply says so. Only if the reply says no dialog could be opened does it hold a confirmation with an `intent_id` printed in its text: then call `mas_request_approval` with that `intent_id`. You only learn the outcome; you cannot answer for the owner.
6. **Report the outcome in the owner's words**: paired, or not approved and nothing was stored.

## Remove a computer ("unpair")

1. Call `mas_unpair` with `machine` set to the paired computer's name (see `mas_network` for the names). Nothing is removed by this call.
2. The reply holds a confirmation. Call `mas_request_approval` with its `intent_id`. The owner answers in a system dialog.
3. Only an approval removes the computer's key: from that moment it is refused, and if it is connected it is told and forgets this coordinator. To bring it back, pair it again.

## What the harness refuses, and what that means

* `confirmation_required` when you answer a confirmation with `mas_answer`: a trust decision is a confirmation, never a choice you can give.
* `pairing_busy`: another pairing is in progress; wait for it to end or expire (about two minutes), then try again.
* `not_waiting`: that computer is not on the waiting list; ask who is waiting again.
* `ambiguous_machine` / `unknown_node`: say which computers match and ask the owner which they mean.

## Things you never do

* You never see the pairing code. Never ask for it, never try to find it, and never type it anywhere yourself.
* Never describe a pairing as done before the confirmation was approved.
* Never recommend which computer to pair or which to remove: the owner chooses.
