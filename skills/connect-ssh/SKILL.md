---
name: connect-ssh
description: Use when a device is to be reached over SSH with the human's own key.
---

# Reaching a device: ssh

SSH is the usual route to a computer. The key is the human's: the harness reads the NAME of the environment variable that holds the key path from `targets.yaml` and never a key itself. Check the route with one read-only connection that only prints the banner or runs a read-only template; the host key must already be known, and a changed host key is a stop, not something to accept.

When the route does not answer, tell the human exactly this and stop: _enable sshd on the device (on Android: install Termux and run sshd), add your own public key, then re-run_. The same sentence is kept in `references/setup-hint.txt`, which the planner reads, so the advice the human sees is the advice written here.
