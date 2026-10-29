---
name: hw-peripherals
description: Use when the cameras, audio devices or printers known to this machine need to be listed.
---

# Cameras, audio devices and printers

Lists cameras, audio inputs and outputs and printers by NAME only. Nothing is opened: no camera is started and no microphone is read, because knowing that a device exists is enough to describe the machine, and reading it would be a different permission.

The audio transport (built-in, USB, Bluetooth) is kept because it separates a laptop's own speakers from something plugged in.
