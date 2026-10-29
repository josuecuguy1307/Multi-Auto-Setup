---
name: net-mdns
description: Use when the services announced on the local network (printers, speakers, smart-home devices, computers) should be listed and named.
---

# mDNS / Bonjour services

Devices announce what they offer on the local network. This skill browses those announcements and works in rounds because each answer decides the next question: first the list of service TYPES in use, then the instances of each type, then where each instance lives (its host and port), then the host's address.

Every round is a set of reads the harness runs after checking them (`dns-sd` through the command policy, a short listening window each). On Linux, where `dns-sd` is usually absent, a tool that uses the zeroconf library collects the same announcements.

What stays and what goes: TXT records are the dangerous part, because they carry device ids, names and addresses. They are reduced to a short whitelist of model fields (`model`, `md`, `ty`, `product`, `manufacturer`, `mfg`, `am`) before anything sees them. Instance and host names are masked of anything that looks like an id. Addresses on loopback are ignored.

A service type is named through the IANA registry, then through `assets/lan_rules.json`. A type neither knows stays unresolved: an honest unknown the ladder can pick up, not a guess.
