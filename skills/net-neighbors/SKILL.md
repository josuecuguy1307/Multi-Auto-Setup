---
name: net-neighbors
description: Use when the hosts this machine has already exchanged packets with on the local network need to be listed, without sending anything.
---

# Neighbors this machine already knows

The ARP (macOS) or neighbor (Linux) table lists the hosts this machine talked to recently, with their hardware address and the interface. Reading it sends nothing, so it is the quiet way to see the local network.

Each entry becomes a host entity with the vendor behind its hardware address, looked up in the public OUI list. A randomized (locally administered) address has no vendor on purpose: phones do this for privacy, and a missing vendor there is not a failure.

The table forgets idle hosts, so absence from it is not proof that a host is not there. The full hardware address is used to derive an id and is never stored.
