---
name: connect-adb
description: Use when a phone or tablet is to be reached over USB with adb.
---

# Reaching a device: adb_usb

adb needs the cable, USB debugging on and the key prompt accepted on the phone: three things only the human can do. The reachability check is `adb devices`; a device listed as `unauthorized` means the prompt has not been accepted yet. Commands go through `adb shell` with the same fixed templates as any other route.

When the route does not answer, tell the human exactly this and stop: _connect the cable, turn on USB debugging in developer options and accept the key prompt on the phone, then re-run_. The same sentence is kept in `references/setup-hint.txt`, which the planner reads, so the advice the human sees is the advice written here.
