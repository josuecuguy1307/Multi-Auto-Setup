---
name: protocol-phone
description: Use when the target is an iPhone or iPad the human owns, enrolled in targets.yaml and attached by cable.
---

# An iPhone the owner enrolled

The phone is read over its cable, read-only, and only after its owner taps Trust. You choose which aspect to read; the table of what can be read is fixed in code and holds the model, the iOS version, the battery, the storage totals and the name as a hash. Nothing that reads messages, photos, contacts or app data exists here.

## identity
Call `ios_info` with aspect `identity`. If it says the phone is waiting for Trust or is locked, tell the human with `ask_human` and wait; do not retry in a loop.

## battery
Call `ios_info` with aspect `battery`.

## storage
Call `ios_info` with aspect `storage`. These are totals, not what is on the phone.

## name
Call `ios_info` with aspect `name`. The name is never shown or stored in clear: only a salted hash is kept.

If the owner declines Trust, the answer is that the phone could not be read. Say that and stop; there is no other way in.
