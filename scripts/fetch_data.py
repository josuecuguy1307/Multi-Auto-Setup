"""One-time setup: download the public ID databases into data/downloads/ (git-ignored).

One GET per file. This is data setup, not scanning. After it runs, every lookup works offline.
Writes data/downloads/MANIFEST.json with the URL, fetch time, size and sha256 of each file.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "data" / "downloads"

NORDIC = "https://raw.githubusercontent.com/NordicSemiconductor/bluetooth-numbers-database/master"
SOURCES = [
    ("usb.ids", "http://www.linux-usb.org/usb.ids", "usb.ids"),
    ("ieee-oui", "https://standards-oui.ieee.org/oui/oui.csv", "oui.csv"),
    ("bluetooth-numbers company ids", f"{NORDIC}/v1/company_ids.json", "bt_company_ids.json"),
    ("bluetooth-numbers service uuids", f"{NORDIC}/v1/service_uuids.json", "bt_service_uuids.json"),
    ("bluetooth-numbers LICENSE", f"{NORDIC}/LICENSE", "bt_numbers_LICENSE.txt"),
    ("iana service names", "https://www.iana.org/assignments/service-names-port-numbers/"
     "service-names-port-numbers.csv", "iana_services.csv"),
]


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    manifest = {}
    failed = 0
    with httpx.Client(timeout=90, follow_redirects=True,
                      headers={"User-Agent": "envrec-data-setup/0.1 (one fetch per file)"}) as client:
        for name, url, filename in SOURCES:
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"FAILED  {name}: {type(exc).__name__}: {str(exc)[:120]}")
                failed += 1
                continue
            (DEST / filename).write_bytes(resp.content)
            manifest[filename] = {
                "name": name, "url": url, "final_url": str(resp.url),
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "bytes": len(resp.content), "sha256": hashlib.sha256(resp.content).hexdigest(),
            }
            print(f"ok      {name}: {len(resp.content):,} bytes -> {filename}")
    (DEST / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
