---
name: lan-sweep
description: Use when every host on this machine's own subnet should be found by pinging it once, after the human approves exactly that.
---

# Ping sweep of the own subnet

Pings each address of the subnet this machine is on once, then reads the neighbor table, which now holds every host that answered. It is the one skill that makes a machine learn about hosts that were quiet.

It is active, so it is read tier and asks the human first, showing exactly what it will do: how many addresses, which interface, how many per second and for how long. The scope guard allows only private (RFC 1918) or link-local addresses on this machine's own interface subnets, up to a /22, rate limited, and refuses any host name that resolves to a public address. Nothing leaves the subnet.

Hosts that drop ICMP stay unseen; a count here is a lower bound.
