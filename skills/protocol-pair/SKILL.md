---
name: protocol-pair
description: Use when a new computer of the owner has to join the network and the owner is at both machines.
---

# Pairing a node with the coordinator

The owner runs `mas pair` on the coordinator: a fresh random 6-digit code appears there and only there. On the node the owner types the coordinator's address and the code once (`mas pair HOST:PORT`).

Underneath, the code is the password of a password-authenticated key exchange, so it never travels and cannot be ground offline. Each side then proves it holds the same key (a confirmation bound to the whole exchange and to the certificate the node saw). The coordinator shows the node's name, operating system and fingerprint, and the OWNER says yes. Only then do both sides store each other's identity keys.

One attempt per code, two minutes at most. A wrong code, a second try, a late answer or a refusal ends the protocol and stores nothing. The code is never written to an event, a log or a fact.

## offered, exchange, owner_confirm
The states are enforced in code (`envrec_net.protocol`): a move needs the evidence its rule names, and any other move is refused and logged.
