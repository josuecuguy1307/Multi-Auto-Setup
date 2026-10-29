---
name: net-interfaces
description: Use when this machine's own network addresses, default gateway or DNS servers are needed to understand which network it is on.
---

# Own interfaces, default route and DNS

This is the machine looking at itself: which interfaces hold an address, which one is the default route and which DNS servers it asks. It is the starting point for everything that touches the local network, because the network scope guard derives the only subnets that may be scanned from these interfaces.

Loopback and link-local addresses are left out of the address list: they say nothing about the network. The addresses become entities owned by this machine (one per interface), the gateway and DNS become facts.

Nothing is sent. The answer is only as fresh as the run.
