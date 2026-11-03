"""Write `domains` and `domains_source` into the shipped catalog entries from what their own sources state (REAL fetches, kept by hash). Run it again to refresh.

    python3 scripts/tag_domains.py [--write]"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from envrec_coordinator import domain_tags as D  # noqa: E402
from envrec_net import envrun as E  # noqa: E402

FILE = Path(__file__).resolve().parents[1] / "skills" / "router-environments" / "references" / "environments.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    spec, doc, today = D.load(), json.loads(FILE.read_text(encoding="utf-8")), time.strftime("%Y-%m-%d", time.gmtime())
    for e in doc["environments"]:
        if e["id"] in spec["entries"]:
            got = D.build(e["id"], spec, today=today)
            print(e["id"], got["domains"], [m["rule"] for m in got["domains_source"]["matched"]])
            e["domains"], e["domains_source"] = got["domains"], got["domains_source"]
        elif e.get("origin") != "researched":
            print("NO SOURCES LISTED FOR", e["id"])
    if a.write:
        FILE.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
