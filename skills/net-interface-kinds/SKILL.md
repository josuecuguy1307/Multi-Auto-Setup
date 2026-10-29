---
name: net-interface-kinds
description: Use when the network interfaces of this machine have to be sorted into real LAN interfaces and virtual ones (VPN, container bridges, virtual machines) for discovery.
---

# Which interfaces are virtual, per operating system

Discovery sends only on real private-LAN interfaces by default. What a virtual interface is called depends on the operating system, so the names are data in `references/virtual-interfaces.json`: per family, a list of name prefixes and fragments (case-insensitive). The per-OS layer reads the table; code never carries a name of its own. The owner can include virtual interfaces with `--include-virtual`.

Add an entry when a new VPN, container or virtual-machine product shows up under another name.
