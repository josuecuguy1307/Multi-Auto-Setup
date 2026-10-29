"""`envrec data fetch`: download the approved reference tables listed in data/sources.yaml.

For each file it shows the URL and size (a HEAD request), checks the disk reserve, asks for approval through the existing gate,
fetches with one GET, and records the sha256 at fetch time in data/downloads/MANIFEST.json. Files that are already present and match
their recorded hash are skipped unless --refresh is given.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import httpx
import yaml

from envrec.contracts import Origin
from envrec.remote.approval import ApprovalGate, PendingAction
from envrec.store import Store

ROOT = Path(__file__).resolve().parents[2]
RESERVE_GIB = 4.0


def load_sources(path: Path | None = None) -> list[dict]:
    return (yaml.safe_load((path or ROOT / "data" / "sources.yaml").read_text(encoding="utf-8")) or {}).get("sources", [])


def fetch_all(dest: Path, gate: ApprovalGate, *, sources: list[dict] | None = None, refresh: bool = False, client: httpx.Client | None = None,
              reserve_gib: float = RESERVE_GIB, free_fn=lambda p: shutil.disk_usage(p).free) -> dict[str, str]:
    sources = sources if sources is not None else load_sources()
    dest.mkdir(parents=True, exist_ok=True)
    mpath = dest / "MANIFEST.json"
    manifest = json.loads(mpath.read_text()) if mpath.is_file() else {}
    own = client or httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": "envrec-data-fetch (one GET per file)"})
    result: dict[str, str] = {}
    try:
        for s in sources:
            f = dest / s["file"]
            rec = manifest.get(s["file"])
            if f.is_file() and rec and hashlib.sha256(f.read_bytes()).hexdigest() == rec.get("sha256") and not refresh:
                result[s["id"]] = "present"
                continue
            try:
                size = int(own.head(s["url"]).headers.get("content-length", 0))
            except httpx.HTTPError:
                size = 0
            if free_fn(dest) - size < reserve_gib * 2**30:
                result[s["id"]] = f"refused: would leave less than the {reserve_gib:g} GiB disk reserve"
                continue
            pending = PendingAction("fetch_reference_table", f"GET {s['url']}", "controller", Origin.builtin, None,
                                    f"{s['file']} (about {size / 1e6:.1f} MB), license: {s['license']}")
            if not gate.request(pending):
                result[s["id"]] = "denied"
                continue
            try:
                r = own.get(s["url"])
                r.raise_for_status()
            except httpx.HTTPError as exc:
                result[s["id"]] = f"failed: {type(exc).__name__}"
                continue
            f.write_bytes(r.content)
            manifest[s["file"]] = {"name": s["id"], "url": s["url"], "final_url": str(r.url), "license": s["license"], "bytes": len(r.content),
                                   "sha256": hashlib.sha256(r.content).hexdigest()}
            result[s["id"]] = f"fetched {len(r.content):,} bytes"
    finally:
        if client is None:
            own.close()
    mpath.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return result


def cmd_data_fetch(args) -> int:
    dest = Path(args.dest)
    with Store(Path(args.runs_dir) / "data-fetch") as store:
        gate = ApprovalGate(store, (lambda _p: "approve") if args.yes else input, print)
        res = fetch_all(dest, gate, refresh=args.refresh)
    for k, v in res.items():
        print(f"{k:26} {v}")
    return 0 if not any(v.startswith(("failed", "refused")) for v in res.values()) else 1


def add_data_commands(sub) -> None:
    d = sub.add_parser("data", help="reference tables (usb.ids, OUI, Bluetooth numbers, IANA, pci.ids)")
    dsub = d.add_subparsers(dest="data_cmd", required=True)
    f = dsub.add_parser("fetch", help="download the approved tables listed in data/sources.yaml (asks before each file)")
    f.add_argument("--dest", default=str(ROOT / "data" / "downloads"))
    f.add_argument("--runs-dir", default="runs")
    f.add_argument("--refresh", action="store_true")
    f.add_argument("--yes", action="store_true", help="approve every listed file without asking (the list is fixed in data/sources.yaml)")
    f.set_defaults(func=cmd_data_fetch)
