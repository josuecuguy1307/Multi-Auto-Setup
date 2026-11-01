---
name: router-environments
description: Use when the router has to know which environments (provider CLIs, own harnesses, the local model) exist for a task type, what each one needs, which accesses enable it, how to run it without a shell, and what keeps it inside its workspace.
---

# Router environments (data, not code)

`references/environments.json` is the curated catalog: written by the project, never by a model. Each entry has a subtype (`provider`, `own_harness`, `local_model`; `embedded` is declared but has no entry yet), the task types it serves, operating systems, what it needs, its license, its upstream and pin, its `automated_use` state, its accesses (what enables it on a machine, with a cost class and, for a plan, a `plan_group` whose members share a quota counter), and an `adapter` (executable name per operating-system family, the arguments per task type, the environment variables it may receive by NAME, how the answer is read, and the confinement of each mode).

Every field that decides routing is labelled in `labels`: `verified by code` (a program read it) or `declared by the project` (quoted from a document, with its source). A field with no label is not used. `confinement` is the honest state of what keeps the tool inside its workspace: `none_needed` (no tools), `tool_declared` (the vendor documents a switch; not verified by us), `unverified` (nothing documented or not verified; needs our sandbox backend) or `sandbox_required` (the tool says it has no confinement of its own). Only our sandbox backend, or `none_needed`, is trusted without a test of our own.

`references/policy.json` holds the rule's visible configuration (tie margin, cost order, escalation cap) and the defaults of the owner policy until the owner's draft (T3b) provides them: which subtypes are open, which providers are approved for internal work, which environments the owner has cleared for automated use.

A change to either file is reviewed like code. A new environment arrives only through a manifest here with its labels; the router and the node read it, and neither carries a tool name, a flag or a path of its own (checks P1, H1).
