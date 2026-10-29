---
name: protocol-ble
description: Use when the target is a Bluetooth device, paired or only advertising nearby, and what it is has not been worked out.
---

# A Bluetooth device

Two different worlds hide under one word. Paired devices are the owner's: the operating system lists them and you may read the list. Advertisers nearby are other people's: you may count and classify them, and nothing more.

## paired
Read the list the operating system keeps. It names the device, its state and often its type.

## listen
A passive scan only listens. Identifiers are hashed with a salt that changes every run, and advertised names are dropped, so a stranger's device cannot be followed from one run to the next. Never connect, pair or read GATT characteristics: that is outside what you were allowed.

## classify
Company ids and service UUIDs resolve through the Bluetooth numbers. Report the vendor and class, and leave the model unknown unless a list names it.

If macOS stops the scan, the answer is a typed `permission` unknown that names the exact Settings step. Do not retry.
