---
name: serial-probe
description: Use when one serial port must be asked who it is, after the human approves exactly which queries will be sent.
---

# Identify one serial port

Opens ONE serial port exclusively, listens first, and only if it stays silent sends the short fixed list in `assets/serial_probes.json` (a newline, `AT`, `*IDN?`, `$I`), stopping at the first reply. That table is the only source of bytes this skill can ever send; the tool enforces it, and the human approves the exact list for each port.

Listening first matters: GPS receivers and many sensors talk without being asked, and then nothing needs to be sent at all.

The reply is matched against the protocol signatures (NMEA with its checksum, SCPI identity, AT, G-code firmware banners, Modbus frames) by a registered lookup tool. A match is a HYPOTHESIS, with the confidence the signature gives; one more harmless read can confirm it. A port in use by another program answers `denied`: that is a result, and the port is left alone.
