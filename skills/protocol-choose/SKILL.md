---
name: protocol-choose
description: Use when the harness was given several candidate targets and the goal does not say which one to work on.
---

# Choosing which target to work on

You were given a goal and a list of candidates. You are not told which one is the right one: look at what each candidate is (its kind and its handle) and pick the one that fits the goal.

## choose
Move to one candidate with `extend_to` and its `target_id`. Pick one; you can only move on, not back. Do not call `done` here: nothing has been worked out yet.
