---
name: connect-serial
description: Use when a device is to be reached over a serial port.
---

# Reaching a device: serial

A serial route is for devices with no network. The port is opened exclusively; if another program holds it the answer is `denied` and the port is left alone. Sending is limited to the queries `targets.yaml` allows for that port (`allow_send`); without it the route is listen-only.

When the route does not answer, tell the human exactly this and stop: _connect the serial adapter, check the port name and permissions, then re-run_. The same sentence is kept in `references/setup-hint.txt`, which the planner reads, so the advice the human sees is the advice written here.
