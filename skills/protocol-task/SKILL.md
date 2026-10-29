---
name: protocol-task
description: Use when the coordinator asks a paired node to run one read-only task and the result has to come back signed and typed.
---

# A read-only task from the coordinator to a node

The owner (or code, for the node's self-report on connect) asks the coordinator to run a task on ONE connected node. The coordinator sends the task inside the node's session with a capability token it signed: the task, the node, the session, the tool allowed, single use, and a time to live the node measures on its own clock. The node checks the token and its own read-only allowlist, then runs the tool and returns a signed, typed result with evidence.

Any task that is not on the allowlist (a write, a shell command, anything else) is rejected with a typed reason. A node that is not connected gets a failed task at once; tasks are not queued, because a queued task would run in a session its token was not made for. At most a small number of tasks run on a node at once.

## created, sent, accepted, running, done, failed, rejected, timed_out, cancelled
The states are enforced in code (`envrec_net.protocol`); a move without its evidence is refused and logged.
