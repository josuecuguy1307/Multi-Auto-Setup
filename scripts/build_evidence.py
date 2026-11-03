"""Build `skills/setup-options/references/evidence.json` entries from the public sources in evidence_sources.json (REAL fetches from the allowed hosts), keep the pages by hash and print what was dropped and why.

    python3 scripts/build_evidence.py [--write]      without --write it only prints
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from envrec_coordinator import evidence_sources as ES  # noqa: E402
from envrec_coordinator.router import evidence as EV  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    spec = ES.load_sources()
    lock = yaml.safe_load((Path(__file__).resolve().parents[1] / "catalog.lock.yaml").read_text(encoding="utf-8"))
    big = Path(__file__).resolve().parents[1] / "live_results" / "recommender-v3" / "captures"  # a capture too large for the repository stays here (local only); its hash is in the coverage
    out = ES.build(spec, lock, today=time.strftime("%Y-%m-%d", time.gmtime()), keep_big=big)
    bad = [(e["id"], ES.verify(e, spec)[1]) for e in out["entries"] if not ES.verify(e, spec)[0]]
    print(json.dumps({"entries": len(out["entries"]), "dropped": out["dropped"], "skipped": out["skipped"], "requests": out["requests"], "not_verified": bad, "models_skipped_for_the_owner": out["models_skipped"], "coverage": out["coverage"]}, indent=1))
    if a.write and not bad:
        doc = EV.load()
        doc["entries"] = sorted(out["entries"], key=lambda e: e["id"])
        doc["coverage"] = out["coverage"]
        doc["models_without_evidence_for_the_owner"] = out["models_skipped"]
        EV.EVIDENCE_FILE.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        print("written", EV.EVIDENCE_FILE)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
