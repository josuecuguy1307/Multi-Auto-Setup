---
name: protocol-lan
description: Use when computers that discovered this coordinator are waiting to be paired and the owner has to pick which one gets a pairing code.
---

# Pairing offers on the local network (a harness run on the coordinator)

You list the computers that are waiting (`lan_discover_nodes`, read-only evidence), ask the owner which one to pair (`ask_owner`, a closed question made from that list), and only then start the pairing offer for the machine the OWNER picked (`pair_offer_start`). Code refuses the offer without the owner's pick. The key exchange, the signatures and the code itself are code; the owner's approval of the machine comes after.

## look
The state is enforced in code (`envrec.protocols`).
