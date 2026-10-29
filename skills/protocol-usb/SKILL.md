---
name: protocol-usb
description: Use when the target is a USB device attached to this machine and its vendor or product is not yet known.
---

# An attached USB device

Be cheapest first and never claim more than you have tested. Every step below is harmless to the device.

## read
Read the descriptor through the USB skill: vendor id, product id and the name string. These are facts about the device itself.

## guess
Look the ids up in usb.ids. A vendor match names the vendor, not the model: if the list only has the vendor, leave the model unknown. A guess is a hypothesis until a second, independent read agrees with it.

## confirm
Make one confirming read, such as the serial identification query when the device also exposes a serial port. If it agrees, the fact becomes tested. If it disagrees, the fact becomes contradicted. Both are correct outcomes; hiding a contradiction is the only wrong one.

## ask
If every tool is used and something is still unknown, use `ask_human` with the fact key and a clear question. The human can tell you in one line what took you ten turns to fail at.
