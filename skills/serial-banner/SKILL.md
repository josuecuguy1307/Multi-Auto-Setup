---
name: serial-banner
description: Use when a serial device appears and the first line it says is needed to recognize it, before anything else is tried.
---

# The first line a serial device says

Recognizing a device again is cheap if its first line is known: a GPS prints a sentence, a lab instrument answers `*IDN?`, a hobby sensor prints its own frame. This skill records that first line as `serial.banner` (cleaned, at most 60 characters), whether the device answered at all, and the baud rate it answered at. A skill written earlier for this kind of device matches on that line and runs with no model call.

It uses the same exchange as `serial-probe`: the port is opened exclusively, the device is heard first, and only then are the fixed queries sent (a newline, `AT`, `*IDN?`, `$I`), stopping at the first reply. Because it can send, it is read tier and asks the human first, showing exactly those queries.

Silence is a result: a device that says nothing leaves `serial.banner` unknown, with the reason. Do not retry it; ask the human what the device is.
