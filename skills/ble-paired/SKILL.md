---
name: ble-paired
description: Use when the Bluetooth controller of this machine and the devices paired with or connected to it need to be listed.
---

# Paired and connected Bluetooth devices

Reads what the operating system already knows: whether the Bluetooth controller is on and which chipset it is, and the list of paired devices with their name, type and whether each is connected. These are the OWNER's devices, so they become owned entities.

The company behind a device (from its vendor id) is looked up in the public Bluetooth company list. Device addresses are used only to derive an id and are hashed; they are never stored.

This reads a list; it never connects to anything and never pairs.
