---
name: usb-devices
description: Use when the USB devices attached to this machine need to be listed and their vendor and product names resolved.
---

# Attached USB devices

Lists each attached USB device with its vendor id, product id and name, and resolves the ids through the public usb.ids list. The ids are the device's own claim; usb.ids turns them into names a person can read.

Resolution is a separate, registered lookup tool, asked for once per distinct id pair (a second round): the script never opens the table. A vendor match names the vendor, not the model; the product name is kept only if the list has it. A device the list cannot name stays unresolved and becomes an unknown for the ladder. If the table itself is missing, that is its own typed unknown with the command that fixes it (`envrec data fetch`).

The serial number, when the device has one, is used only to derive a stable id and is hashed; it is never stored. The root hubs Linux lists are skipped.
