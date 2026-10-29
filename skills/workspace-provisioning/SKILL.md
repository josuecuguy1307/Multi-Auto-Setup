---
name: workspace-provisioning
description: Use when a model server has to be put on another device the human owns, run there, checked and removed again.
---

# Putting a model server on another device, and taking it off

The sequence is fixed and every step that writes needs its own fresh approval showing the exact command: check the device (read-only), pick the model size, send the pinned worker and the model with checksums, start the server bound to localhost on the device with memory and disk caps, ask it for its health, and, at the end or on any failure, stop it and remove everything that was installed.

Why the order matters: nothing is written until the read-only check says the device can run it; the server listens only on the device's own loopback and is reached through a tunnel the controller opens, so nothing on the network can talk to it; and the undo is a recipe like any other, run until it confirms nothing remains (no files, no process, the port closed, the disk space back).

What stays in code and is not up for judgment: the allowed targets and routes, the fixed install root, the fixed command templates (a value is placed into a command only after its own schema accepts it), the size and disk limits, the denylist of destructive words, and the rule that a recipe a model invented is refused on a real device until a verifier has passed it.

Your judgment is in the explanation: show the human what will change and how it will be undone, in plain words, before asking.
