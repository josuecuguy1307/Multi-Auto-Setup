---
name: protocol-host
description: Use when the target is the machine the harness runs on and nothing yet says what kind of machine it is.
---

# This machine

The goal is to know what machine you run on before anything else, because the kind of machine decides which worlds are worth looking at: a laptop has rich attached hardware and a rich network; a small board with no screen has a world made of sensors.

## basics
Read the operating system, the memory and the disks. Two sources that agree are worth more than one, so cross-check what matters with the native tools (`read_os_native`, `read_hw_native`). A fact marked unknown means the probe tried and could not tell: do not call that probe again, the answer will not change.

## context
Battery, display, virtualization, installed developer tools and network interfaces tell a laptop from a server from a container. Run each once.

## judge
`classify_host` turns the facts into one judgment. It needs the context step done first, which is why the menu hides it until then. Treat its answer as a hypothesis with the evidence listed beside it.

## survey
The discovery skills read what is attached and what is on the network. Choose the ones the host kind makes worth the time; a skill whose facts are all known is not offered again.

Move on with `advance` when the card says you can. The loop refuses to advance without the evidence and counts the refusal, so asking early costs a turn and nothing else.
