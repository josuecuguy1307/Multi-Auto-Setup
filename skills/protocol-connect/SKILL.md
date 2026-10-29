---
name: protocol-connect
description: Use when the harness must reach another device the human owns, by a route that targets.yaml allows, to look around or to put a worker there.
---

# Connecting to another device

Reaching another device is the most consequential thing this harness does, so the policy lives in code and not in this text: the allowed targets and transports come from `targets.yaml`, which only the human edits; the approval gate shows the exact plan; the size and disk limits are fixed; destructive words are refused; every write has an undo.

What this skill adds is judgment: which route fits the device, what to read first, how to explain the plan so the human can say yes with their eyes open.

## route
Use the transport the device's entry allows (ssh, adb over USB, serial, docker exec). If none is allowed, the answer is "not allowed": say so and stop.

## plan
List every command and every file written, with the undo for each. A plan that cannot be undone is not a plan.

## approve and run
Wait for the human. Run only what the approval showed. If a step fails, stop and report: do not improvise a repair.

## verify
Check from the controller's side that the thing works (a health endpoint, a version string), then record it.
