"""Write the replay-device fixtures. They are hand-written transcripts, not captures from hardware.

Each replay device stands in for a USB-serial gadget: a USB descriptor, an unsolicited stream the
device emits, and canned replies to a few safe identification commands. Truth is known by
construction, so ground_truth/replay-*.yaml can be marked confirmed.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "fixtures" / "replay"
SAMPLES = ROOT / "fixtures" / "samples"


def nmea(body: str) -> str:
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"${body}*{x:02X}\r\n"


DEVICES = {
    # (a) matches a public-database entry; the serial stream is plain NMEA
    "ch340-gps": {
        "name": "ch340-gps",
        "handle": "replay:ch340-gps",
        "kind": "usb",
        "descriptor": {"vendor_id": "1a86", "product_id": "7523", "manufacturer": "QinHeng Electronics"},
        "stream": [
            nmea("GPGLL,4916.45,N,12311.12,W,225444,A"),
            nmea("GPGLL,4916.46,N,12311.13,W,225445,A"),
            nmea("GPGLL,4916.47,N,12311.14,W,225446,A"),
        ],
        "responses": {},
    },
    # (b) nothing recognizes it: vendor id is not in usb.ids, the stream matches no signature, it ignores queries
    "mystery-box": {
        "name": "mystery-box",
        "handle": "replay:mystery-box",
        "kind": "usb",
        "descriptor": {"vendor_id": "ffff", "product_id": "0001", "manufacturer": None},
        "stream": ["~~ZXQ 7/9 ~~\r\n", "~~ZXQ 7/9 ~~\r\n"],
        "responses": {},
    },
    # (c) looks like NMEA at first; the confirming read shows it is something else (an instrument identity reply)
    "liar-device": {
        "name": "liar-device",
        "handle": "replay:liar-device",
        "kind": "usb",
        "descriptor": {"vendor_id": "0403", "product_id": "6001", "manufacturer": "FTDI"},
        "stream": [
            nmea("GPGLL,4916.45,N,12311.12,W,225444,A"),
            "ACME INSTRUMENTS,PSU-3000,SN0042,v1.2\r\n",
            "ACME INSTRUMENTS,PSU-3000,SN0042,v1.2\r\n",
        ],
        "responses": {},
    },
    # (d) unknown to every list, but a manual exists: the case rungs 4 and 5 are for
    "zxq-sensor": {
        "name": "zxq-sensor",
        "handle": "replay:zxq-sensor",
        "kind": "usb",
        "descriptor": {"vendor_id": "ffff", "product_id": "0002", "manufacturer": None},
        "stream": ["~~ZXQ 7/9 ~~\r\n", "~~ZXQ 8/9 ~~\r\n", "~~ZXQ 7/10 ~~\r\n"],
        "responses": {},
    },
    # (e) a second unit of the same model: same protocol, different readings
    "zxq-sensor-b": {
        "name": "zxq-sensor-b",
        "handle": "replay:zxq-sensor-b",
        "kind": "usb",
        "descriptor": {"vendor_id": "ffff", "product_id": "0002", "manufacturer": None},
        "stream": ["~~ZXQ 21/55 ~~\r\n", "~~ZXQ 22/54 ~~\r\n", "~~ZXQ 21/53 ~~\r\n"],
        "responses": {},
    },
    # (f) same USB ids, different firmware: it speaks another dialect, so a saved skill must NOT carry over
    "zxq-v2": {
        "name": "zxq-v2",
        "handle": "replay:zxq-v2",
        "kind": "usb",
        "descriptor": {"vendor_id": "ffff", "product_id": "0002", "manufacturer": None},
        "stream": ["~~ZXQ2 t=7;h=9 ~~\r\n", "~~ZXQ2 t=8;h=9 ~~\r\n", "~~ZXQ2 t=7;h=10 ~~\r\n"],
        "responses": {},
    },
}

USB_SAMPLE = """\
# Small labeled sample of usb.ids entries (copied from the real list; the full file is not committed).
# Version: sample-1
1a86  QinHeng Electronics
\t7523  CH340 serial converter
0403  Future Technology Devices International, Ltd
\t6001  FT232 Serial (UART) IC
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    SAMPLES.mkdir(parents=True, exist_ok=True)
    for name, device in DEVICES.items():
        device = {**device, "synthetic": True,
                  "note": "hand-written transcript for tests; not a capture from real hardware"}
        (OUT / f"{name}.json").write_text(json.dumps(device, indent=2) + "\n", encoding="utf-8")
        print("wrote", OUT / f"{name}.json")
    (SAMPLES / "usb_ids_sample.txt").write_text(USB_SAMPLE, encoding="utf-8")
    print("wrote", SAMPLES / "usb_ids_sample.txt")


if __name__ == "__main__":
    main()
