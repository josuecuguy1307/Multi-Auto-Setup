---
name: net-listening
description: Use when you need to know which ports this machine is listening on and which program owns each, for example to find a local server.
---

# Listening TCP ports

Lists the TCP ports this machine accepts connections on and the name of the program that owns each, marking each as reachable only from this machine (loopback) or from the network.

Only program names are kept. Command lines are never read: they routinely carry tokens and paths. Other users' processes can be invisible without privileges, so an absent entry is not proof that nothing listens there.

Use it before looking for a local model server or a database, and to explain a port that appears in a network scan of this machine.
