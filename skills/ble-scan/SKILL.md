---
name: ble-scan
description: Use when the Bluetooth LE devices advertising nearby should be counted and classified by vendor and service, without connecting to any.
---

# Nearby Bluetooth LE advertisers

A passive listen for a few seconds. Each advertiser is kept only as a vendor (from its company id) and a service class (from its service UUIDs), both resolved through the public Bluetooth lists, plus a coarse `near` or `far` from the signal strength.

The privacy rules are the point of this skill. The scan runs in its own process; identifiers are hashed there with a per-run salt, so the same stranger's device gets a different id on the next run and cannot be followed. Advertised names are dropped before anything is written. Only counts and classes survive. Never connect, pair or read GATT characteristics.

If macOS stops the scan because the program has no Bluetooth permission, the result is a typed `permission` unknown that names the exact Settings step. Report it once and do not retry.
