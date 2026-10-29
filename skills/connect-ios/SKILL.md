---
name: connect-ios
description: Use when an iPhone or iPad the human owns is to be read over a USB cable with libimobiledevice.
---

# Reaching a device: ios_usb

An iPhone answers over USB only after its owner taps Trust on the phone and the phone is unlocked. Those are things only the human can do; the harness never tries to get around the Trust prompt, the pairing or the passcode, and if the owner does not trust this computer it stops there and says so.

The route is read-only and fixed: `idevice_id -l` lists the attached devices (the entry names its phone by the SHA-256 of the identifier, never the identifier), and `ideviceinfo -k KEY` reads one value from a short table (model, iOS version, battery, storage totals, name as a hash). Nothing that reads messages, photos, contacts or app data is in the table.

When the route does not answer, tell the human exactly this and stop: _connect the cable, unlock the phone and tap Trust when it asks, then re-run (libimobiledevice must be installed: `brew install libimobiledevice`)_. The same sentence is kept in `references/setup-hint.txt`.
