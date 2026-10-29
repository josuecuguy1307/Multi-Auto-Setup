---
name: connect-route
description: Use when the best way to reach one named device has to be chosen from the routes the human allowed for it.
---

# Choosing the route to a device

The human decides which routes are allowed for each device, in `targets.yaml`. Your part is to choose among THOSE, in the order that fits what is already known about the device, and to say why. You never try a route that is not listed, and you never look for another way in: if none of the allowed routes answers, say what the human can set up and stop.

`references/route-table.json` holds the preference order: a container is reached with `docker exec` first, a phone with `adb` over USB, an instrument over its network port, a single-board computer over SSH, an ordinary Linux or macOS machine over SSH. A rule fires on facts the harness already has (`host.kind`, `os.name`); the facts that decided the choice are recorded with the choice.

Before choosing, each allowed route gets one read-only reachability check (a list of the container, `adb devices`, an SSH banner, the serial port's presence). A route is chosen only if its check answered. The check never sends a command that changes anything.

The per-route skills (`connect-ssh`, `connect-adb`, `connect-docker-exec`, `connect-serial`, `connect-tcp`) say what to look at for each route and what the human has to do when it does not answer.
