---
name: lan-ssdp
description: Use when UPnP devices on the local network (routers, media renderers, gateways) should be found by asking the network once, after the human approves.
---

# SSDP search

Sends ONE multicast M-SEARCH and waits a few seconds, then reads the description document of each device that answered. It is active, so it is read tier and asks first, naming the group, the wait and the subnet the descriptions may come from.

Safety rules that live in the tool, not here: a description is fetched only from the address that answered and only if it is inside the scope; an answer from outside is ignored, never followed. The description is read WITHOUT an XML parser (a hostile document with entity expansion must never run), and serial numbers and UDNs are not read at all.

The device type and the SSDP search targets are matched against `assets/lan_rules.json` for a plain-words meaning (gateway, media renderer). No match leaves the device unresolved.
