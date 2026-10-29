---
name: serial-ports
description: Use when the serial ports of this machine (USB-serial adapters, UARTs) should be listed by name without opening any of them.
---

# Serial ports by name

Lists the serial devices under `/dev` by name and says which kind they look like (USB serial or UART), linking each USB-serial one to its USB device when the name carries the same tail. NOTHING is opened: knowing a port exists is enough here, and opening one is a separate, approved step.

`assets/serial_ports.json` says which name patterns count as serial ports and which are denied (Bluetooth audio ports, debug consoles). Entries that are denied are counted but never listed or opened, so the count says how many were seen and ignored.
