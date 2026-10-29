---
name: connect-tcp
description: Use when an instrument is to be reached on a TCP port of the owner's own network.
---

# Reaching a device: tcp

A network instrument is reached on one address and port from `targets.yaml`, inside the owner's own subnet. The check is a plain connect; the only data sent afterwards is what the target's configuration allows. Nothing outside the owner's network is ever contacted.

When the route does not answer, tell the human exactly this and stop: _power the instrument, check its address and port on your own network, then re-run_. The same sentence is kept in `references/setup-hint.txt`, which the planner reads, so the advice the human sees is the advice written here.
