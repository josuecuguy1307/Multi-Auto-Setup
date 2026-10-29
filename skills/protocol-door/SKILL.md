---
name: protocol-door
description: Use when a computer the owner owns is not enrolled yet and the harness must find a way in with the owner's own gestures.
---

# The door to a computer that is not enrolled yet

LEGACY: this is the retired inbound-SSH route (`docs/NETWORK_CONTRACT.md` 1.5 and 2.5). New computers join with `protocol-pair`; nothing in the `mas` commands uses this protocol.

Every kind of computer has a door made for its owner. For a Windows computer reached over SSH the harness makes a key, serves ONE setup file on its own private network, and the owner opens the link on the other computer, clicks Yes at the administrator prompt and types the 6 digits it shows. The file reports back who the computer is (user, name, version, SSH host key) and the address comes from the connection itself: the owner never types an address or a user name.

## key, serve
Make the key. Serve the file; the owner sees a short link and a QR code.

## wait
If nobody opens the link, the two computers cannot reach each other (a shared network can isolate clients). Say so plainly and suggest a network where both can reach each other. Do not scan or probe the network.

## pair, propose
The digits are checked against the host key the harness sees. Then a proposal of the target is written; only the owner's typed name and yes in enrollment makes it a target.
