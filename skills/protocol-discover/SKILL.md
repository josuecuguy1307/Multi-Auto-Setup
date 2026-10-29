---
name: protocol-discover
description: Use when a computer that has the program installed has to find the coordinators on its own network and join one without anyone typing an address.
---

# Discovery and waiting to be paired

The node sends one small versioned query from an ephemeral UDP port on each private LAN interface, listens for the answers to that query only, and closes the socket when the window ends. A coordinator answers with its name, address, TCP port and fingerprint; before pairing that is only a hint, because pairing itself is protected by the key exchange and the owner's approval. After pairing, the node accepts only an answer signed by its pinned coordinator.

The OWNER picks which coordinator to join (several may answer; nothing joins silently). The node then dials it and registers as waiting with its name, operating system, architecture and claimed fingerprint, and nothing else. A code exists only after the owner presses pair for this machine on the coordinator; it is good for this machine's own connection only.

If nothing answers, the owner gets plain guidance in their operating system's words (`discovery-guidance`); the software changes no firewall, network profile or router setting.

## searching, listed, picked, waiting, ready, empty
The states are enforced in code (`envrec_net.protocol`); a move without its evidence is refused and logged.
