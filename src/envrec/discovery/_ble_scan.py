"""Runs in its OWN process: a passive Bluetooth LE advertisement scan with bleak. On macOS an app without Bluetooth permission is aborted by the
OS the moment it touches CoreBluetooth, so the scan must never share a process with the harness. Prints reduced JSON; identifiers are salted-hashed here."""

import asyncio
import hashlib
import json
import sys


def short_hash(value: str, salt: str) -> str:
    return hashlib.sha256((salt + "\x1f" + value).encode()).hexdigest()[:8]


async def main(seconds: float, salt: str) -> None:
    import bleak
    found = await bleak.BleakScanner.discover(timeout=seconds, return_adv=True)
    recs = [{"id": short_hash(dev.address, salt), "rssi": adv.rssi, "company": sorted(int(k) for k in (adv.manufacturer_data or {})),
             "services": sorted(str(u).lower() for u in (adv.service_uuids or [])), "tx": adv.tx_power} for dev, adv in found.values()]
    print(json.dumps(recs))


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]), sys.argv[2]))
