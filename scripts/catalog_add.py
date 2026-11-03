"""Add the models of a few approved sources to the shipped lock WITHOUT regenerating everything (a full regeneration would move the pins of every existing row): the generator runs on the manifest reduced to
the named model sources, and its rows and exclusions are merged into catalog.lock.yaml (pairs computed by the same matrix rule). Then scripts/catalog_verify.py checks the whole lock against the hub, the files'
GGUF headers and the pinned llama.cpp build.

    python3 scripts/catalog_add.py SOURCE_ID [SOURCE_ID ...] [--lock FILE] [--manifest FILE] [--cache DIR] [--excluded FILE] [--write]"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from envrec.catalog import generate  # noqa: E402
from envrec.catalog.generate import render_excluded  # noqa: E402
from envrec.catalog.live import LiveMetadata  # noqa: E402
from envrec.runtime.manifest import Manifest  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="+")
    ap.add_argument("--lock", default=str(ROOT / "catalog.lock.yaml"))
    ap.add_argument("--manifest", default=str(ROOT / "config" / "runtime_manifest.yaml"))
    ap.add_argument("--excluded", default=str(ROOT / "catalog.excluded.md"))
    ap.add_argument("--cache", default=None)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    raw = yaml.safe_load(Path(a.manifest).read_text())
    small = copy.deepcopy(raw)
    small["runtime_sources"] = []
    small["model_sources"] = [s for s in raw["model_sources"] if s["id"] in a.sources and s.get("status") == "approve"]
    if {s["id"] for s in small["model_sources"]} != set(a.sources):
        print("some source is not approved in the manifest:", set(a.sources) - {s["id"] for s in small["model_sources"]}, file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp()) / "m.yaml"
    tmp.write_text(yaml.safe_dump(small))
    gen = generate(Manifest.load(tmp), LiveMetadata(github_token=os.environ.get("GITHUB_TOKEN"), pace_s=0.25, cache_dir=Path(a.cache) if a.cache else None))
    lock = json.loads(Path(a.lock).read_text())
    have = {m["id"] for m in lock["model_files"]}
    new = [m.model_dump(mode="json") for m in gen.lock.model_files if m.id not in have]
    lock["model_files"] = sorted(lock["model_files"] + new, key=lambda m: m["id"])
    pairs = {tuple(p) for p in lock["pairs"]}
    for m in new:
        for r in lock["runtime_assets"]:
            if m["format"] in r["formats"]:
                pairs.add((r["id"], m["id"]))
    lock["pairs"] = sorted(pairs)
    seen = {(e["what"], e["reason"]) for e in lock["excluded"]}
    added_ex = [e for e in gen.lock.excluded if (e["what"], e["reason"]) not in seen and not e["what"].startswith("source ")]
    lock["excluded"] += added_ex
    lock["counts"] = {**lock["counts"], "model_files": len(lock["model_files"]), "combinations": len(lock["pairs"]), "excluded": len(lock["excluded"])}
    print(json.dumps({"new_model_files": [m["id"] for m in new], "excluded": added_ex}, indent=1))
    if a.write:
        Path(a.lock).write_text(json.dumps(lock, indent=1) + "\n", encoding="utf-8")
        Path(a.excluded).write_text(render_excluded(lock["excluded"]), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
