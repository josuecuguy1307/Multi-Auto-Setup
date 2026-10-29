---
name: protocol-node-connect
description: Use when a paired node has to stay connected to its coordinator and report that it is alive.
---

# A node's connection to its coordinator

The node only dials out, on the channel pinned at pairing; it opens no listening socket. It reads its card basics (host name, operating system, architecture), connects, sends a signed heartbeat on a timer, and checks the coordinator's signed acknowledgement. If the connection is lost it waits with growing delays and dials again.

It executes nothing for the coordinator in this version: read-only.

## card, connect, online, backoff
The states are enforced in code (`envrec_net.protocol`); a move without its evidence is refused and logged.
