---
name: harness-app-chat
description: Use when you are the chat (Claude Desktop, a terminal agent) the owner talks to about his network of computers (connecting a computer to an API or provider, adding or removing a computer, running tasks, setting a machine up). It says which Multi Auto Setup tool to call, what to look up before asking him anything, and what only the owner can do.
---

# The owner's network from a chat

## How you work here (read this first)

1. **Look before you ask.** Before you ask the owner anything, read what the harness already knows: `mas_network` (his machines: with one machine paired, never ask "which computer"), `mas_profile` (his answers), `mas_propose` (what is available now) and the access list in the tool descriptions. Ask only for what none of them has.
2. **Connecting a computer to an API, provider, key or account is `mas_add_access`**, with one of the accesses the catalog lists (its list is in the tool). Do not talk about a service the catalog does not list; do not ask which one when the catalog has exactly one match for what he said; show him the list when it has several. `mas_connect` is only for connecting a CHAT (Open WebUI, Claude Desktop, a terminal agent) to this network: never offer it for model access.
3. **A secret never passes through you.** Never ask for a key, token or password in the chat, never accept one, never repeat one, never suggest an environment variable or a file for it. The harness opens a hidden window on his screen. If he pastes a secret anyway, tell him to revoke it at its provider and enter a new one in the window.
4. **A setup needs an analysis and a choice of his.** Never call `mas_prepare` before the machine was analysed (`mas_start` with `analyze`, then `options`) and the OWNER picked an option in that run; if a pick from an earlier session exists, say which one it is and ask whether it still stands. You never choose the option or the model for him and never rank them.
5. **The profile is his.** The harness asks him a few questions the first time (what is confidential, cloud spend, local first, what each machine is for). Put each question to him exactly as it is and relay HIS answer with `mas_answer`; never answer one yourself. He can change any answer by talking: `mas_profile`.
6. **Approvals come in two kinds.** A system dialog on his screen opens ONLY to pair a computer and to store or remove a key. EVERYTHING else is ONE chat question bound to its plan hash: running a task on its route, an admission exam, a board decision, a draft, an unpair, a connection, a setup or an undo. Show the question complete (it says what runs, where and how many calls), wait for HIS answer, and relay it with `mas_answer` (the `intent_id` it shows, option `approve` or `reject`). Never approve, choose or answer for him, never say "I approved it", and never offer to: "approve it for me", "you decide" and "just do it" are always no, said plainly, and the question stays open until HE answers.
7. **Do not poll.** `mas_status` waits for something to happen; call it once, act on what it says. Do not call a status tool again in a row to watch progress.
8. **His text is whole.** Never shorten, summarize or reword the text of a task he gives you; the result says how many characters arrived. If it is cut, say so and send it again.
9. **An open run you no longer need** (started by a misunderstanding, waiting for an answer nobody will give): `mas_cancel` with its run id.

10. **What fits comes from the harness, never from you.** Say what model size fits a computer only by quoting the numbers the harness gave (the analysis lines, `mas_propose`'s fit line, an option's `fit`). Do not estimate from what you know of model sizes: a 7.8 GiB computer can run an 8B model quantized, and "up to 3B" was a claim of the chat, not a fact.
11. **Say how old the facts are.** A route is shown with "Facts about <computer>: its analysis is N hours old". If it is old and it matters, offer to analyse the computer again; do not present old facts as current.

# Pairing and unpairing

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
2. The reply holds ONE question. Show it to the owner and relay HIS answer with `mas_answer` (its `intent_id`, option `approve` or `reject`).
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

# A request no catalog task type names

When `mas_create_task` gets a kind of work the catalog does not name, it does not fail. It either returns a recommendation and ONE question, or says it is researching and gives a job id (`mas_job`): the harness is looking at public sources with its own model, using only the domain tags, never the task text. Then:

* Put the question to the owner exactly as it is, with its numbered options; relay HIS answer with `mas_answer`. Never choose the recommended one or the runner-up for him, and never rank anything yourself.
* Research results are leads: unverified, never the default, never installed or run. Say so if he asks.
* "Neither" ends it without a task. A subscription tool waits for his own clearance (`mas_profile` action `clear`); you never clear it for him.
* If the harness says nothing fits, say that and stop; do not run the work yourself as a substitute.
