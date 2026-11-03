"""Turn a captured GitHub release (the API's JSON) into the `upstream.assets` block of a catalog entry, by code: for each wanted key the asset's address, size and the SHA-256 GitHub itself reports for it
(`digest`). Nothing is typed by hand. With --check it compares an entry's pins with the capture.

    python3 scripts/pin_release_assets.py RELEASE.json KEY=ASSET_NAME [KEY=ASSET_NAME ...]
    python3 scripts/pin_release_assets.py RELEASE.json --check ENTRY_ID
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def pins(release: dict, wanted: dict[str, str]) -> dict[str, dict]:
    by = {a["name"]: a for a in release["assets"]}
    out = {}
    for key, name in wanted.items():
        a = by[name]
        if not str(a.get("digest", "")).startswith("sha256:"):
            raise SystemExit(f"{name}: the release reports no SHA-256 digest")
        out[key] = {"name": name, "sha256": a["digest"].split(":", 1)[1], "bytes": a["size"], "url": a["browser_download_url"]}
    return out


def main(argv: list[str]) -> int:
    release = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    if len(argv) > 1 and argv[1] == "--check":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from envrec_net import envrun as E
        entry = E.load_environments()[argv[2]]
        have = entry["upstream"]["assets"]
        bad = [k for k, a in have.items() if pins(release, {k: a["name"]})[k] != {k2: a[k2] for k2 in ("name", "sha256", "bytes", "url")}]
        print("all pins match the capture" if not bad else f"pins that differ: {bad}")
        return 1 if bad else 0
    print(json.dumps(pins(release, dict(x.split("=", 1) for x in argv[1:])), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
