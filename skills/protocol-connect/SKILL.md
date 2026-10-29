---
name: protocol-connect
description: Use when the harness must reach a computer the human owns, check the route, set up a workspace there with the human's approval, verify it and undo it.
---

# Reaching a computer and setting up a workspace there

Reaching another computer is the most consequential thing this harness does, so the policy lives in code and not in this text: the target comes from `targets.yaml`, which only the human edits; every write shows its exact command and its undo and waits for the human; the commands that may run are the ones in a recipe whose hash the human approved.

What this text adds is judgment, state by state.

## route
Check that the computer answers over the route its entry allows. If it does not, stop and say what is missing.

## identify
Find the operating system family. The probe skill knows the Unix-like ones. For anything else, read the computer with `remote_read` (each command is shown to the owner and approved; its output is stored by hash and you never see it) and let a skill you write turn those captures into facts.

## plan
Ask for the recipe and the candidate pairs. If no skill has a recipe for this computer, that is the unknown ladder: research, then write one. If there is a recipe but no pair fits, ask the owner: the way in is a proposal for a new runtime source, which only the owner adds.

## research and generate
Read the official documentation the owner put in the documentation folder, then write one skill: probes (read-only commands), a script that turns their output into facts, tests made from the REAL captures, a signature on the operating system fact, and a recipe. The recipe lists every write with its undo, all under one folder, nothing that fetches from the network, changes accounts, services or the firewall beyond that folder, or runs privileged. The owner approves the exact code once, by its hash.

## deploy, verify, task, undo
Choose one pair from the candidates and deploy it; every command asks again. Verification is by code (a fresh token, a real generation, loopback-only, speed and memory). One small task runs through the workspace. Then undo everything and prove nothing is left.
