"""Re-check catalog.lock.yaml against the hub and the runtime's own source, with the rules of `model-catalog-rules`.

    python scripts/catalog_verify.py --report out.json            # dry run: reports, writes nothing
    python scripts/catalog_verify.py --write                      # rewrites catalog.lock.yaml, catalog.excluded.md, runtime_arches.json

Read-only on the network: the hub's listing and repo info at each PINNED revision, and the first few MiB of each GGUF file (its header) by HTTP range request. No weights are downloaded."""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from envrec.catalog.generate import render_excluded  # noqa: E402
from envrec.catalog.hub import Hub  # noqa: E402
from envrec.catalog.rules import RUNTIME_ARCHES_FILE, Rules  # noqa: E402
from envrec.catalog.verify import check_backend_evidence, fetch_runtime_arches, verify_lock  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", default=str(ROOT / "catalog.lock.yaml"))
    ap.add_argument("--manifest", default=str(ROOT / "config" / "runtime_manifest.yaml"))
    ap.add_argument("--cache", default=None, help="directory for hub answers and parsed headers (a re-run asks nothing again)")
    ap.add_argument("--report", default=None)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--no-runtime-arches", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    lock = yaml.safe_load(Path(a.lock).read_text())
    manifest = yaml.safe_load(Path(a.manifest).read_text())
    approved = [s["author"] for s in manifest["model_sources"] if s.get("status") == "approve" and s.get("author")]
    rules = Rules.load()
    cache = Path(a.cache) if a.cache else None
    hub = Hub(cache_dir=cache)
    arches = {} if a.no_runtime_arches else fetch_runtime_arches(lock["runtime_assets"], rules)
    evidence = [] if a.no_runtime_arches else check_backend_evidence(lock["runtime_assets"], rules)
    if any(e["label"] != "verified-by-code" for e in evidence):
        print("a build's backend label is not backed by the runtime's own release workflow:", [e for e in evidence if e["label"] != "verified-by-code"], file=sys.stderr)
        return 2
    new, report = verify_lock(lock, hub, rules, approved_publishers=approved, runtime_arches=arches, checked_on=datetime.date.today().isoformat(),
                              header_cache=(cache / "headers") if cache else None, workers=a.workers, max_params_b=float(manifest.get("max_model_params_b", 0)) or None, backend_evidence=evidence, progress=lambda i: print("  ", i, file=sys.stderr))
    if a.report:
        Path(a.report).write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({k: report[k] for k in ("files", "kept", "problems")}, indent=1))
    print(f"changed fields: {sum(1 for c in report['changes'] if c['change'] in ('params_b', 'bytes'))}; excluded: {sum(1 for c in report['changes'] if c['change'] == 'excluded')}; "
          f"projectors attached or dropped: {sum(1 for c in report['changes'] if c['change'] == 'companion')}")
    if a.write:
        if report["problems"]:
            print("not writing: some files could not be verified (see problems)", file=sys.stderr)
            return 2
        Path(a.lock).write_text(json.dumps(new, indent=1) + "\n", encoding="utf-8")
        Path(ROOT / "catalog.excluded.md").write_text(render_excluded(new["excluded"]), encoding="utf-8")
        if arches:
            RUNTIME_ARCHES_FILE.write_text(json.dumps(arches, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
