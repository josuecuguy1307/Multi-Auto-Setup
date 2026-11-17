"""The acceptance oracle for the MAS portable device: the only thing that may turn a criterion to PASS.

    python scripts/device_accept.py run [--phase N] [--id ID ...]   run criteria, write test-results.json and the ACCEPTANCE.md table
    python scripts/device_accept.py verify                          exit 1 if any recorded PASS is stale (its covered files changed) or the files were hand-edited
    python scripts/device_accept.py status                          print the table; exit 0 only when every criterion is PASS

Default verdict is FAIL. A criterion is PASS only when every test it lists exists, was collected, ran, and passed: a skip, an error, a missing file
or zero tests is FAIL. A PASS records the content hash of the files it covers; when they change it is reported STALE (and counts as not passing)
until it is run again. Output is short on purpose; full pytest output goes to runs/acceptance/<id>.log (search it for ERROR / FAILED).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRITERIA = ROOT / "device" / "acceptance" / "criteria.yaml"
RESULTS = ROOT / "test-results.json"
ACCEPTANCE = ROOT / "ACCEPTANCE.md"
LOGS = ROOT / "runs" / "acceptance"
BEGIN, END = "<!-- BEGIN GENERATED: device_accept.py -->", "<!-- END GENERATED: device_accept.py -->"
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".venv", "runs"}


def load_criteria(path: Path = CRITERIA) -> list[dict]:
    import yaml
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    out = []
    for c in doc["criteria"]:
        for k in ("id", "phase", "title", "tests", "covers", "label"):
            if k not in c:
                raise ValueError(f"criterion {c.get('id', '?')} has no {k!r}")
        if c["id"] in seen:
            raise ValueError(f"duplicate criterion id {c['id']}")
        seen.add(c["id"])
        out.append(c)
    return out


def _files(rel: str, root: Path) -> list[Path]:
    p = root / rel.split("::", 1)[0]
    if p.is_file():
        return [p]
    if p.is_dir():
        return sorted(f for f in p.rglob("*") if f.is_file() and not (SKIP_PARTS & set(f.relative_to(root).parts)))
    return []


def fingerprint(c: dict, root: Path = ROOT) -> str:
    """sha256 over (path, content hash) of every covered file and test file. A missing path hashes as missing, so adding it later changes the print."""
    h = hashlib.sha256()
    for rel in sorted(set(c["covers"]) | {t.split("::", 1)[0] for t in c["tests"]}):
        files = _files(rel, root)
        if not files:
            h.update(f"{rel}\0<missing>\n".encode())
        for f in files:
            data = f.read_bytes().replace(b"\r\n", b"\n")  # the same commit checked out on Windows (autocrlf) and Linux hashes the same
            h.update(f"{f.relative_to(root).as_posix()}\0{hashlib.sha256(data).hexdigest()}\n".encode())
    return h.hexdigest()


def _junit_counts(xml_path: Path) -> dict:
    tree = ET.parse(xml_path)
    agg = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    suites = [tree.getroot()] if tree.getroot().tag == "testsuite" else tree.getroot().findall("testsuite")
    for s in suites:
        for k in agg:
            agg[k] += int(s.get(k, 0))
    return agg


def run_criterion(c: dict, root: Path = ROOT, python: str = sys.executable) -> dict:
    fp = fingerprint(c, root)
    base = {"status": "FAIL", "phase": c["phase"], "title": c["title"], "label": c["label"], "fingerprint": fp, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    missing = [t for t in c["tests"] if not (root / t.split("::", 1)[0]).exists()]
    if not c["tests"]:
        return base | {"reason": "no tests listed"}
    if missing:
        return base | {"reason": "missing test files: " + ", ".join(missing)}
    logs = root / "runs" / "acceptance"
    logs.mkdir(parents=True, exist_ok=True)
    xml = logs / f"{c['id']}.xml"
    xml.unlink(missing_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    cmd = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfEs", f"--junitxml={xml}", *c["tests"]]
    with (logs / f"{c['id']}.log").open("w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, cwd=root, stdout=log, stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL)
    if not xml.exists():
        return base | {"reason": f"pytest exited {proc.returncode} without a report (see runs/acceptance/{c['id']}.log)"}
    n = _junit_counts(xml)
    base["counts"] = n
    if n["tests"] == 0:
        return base | {"reason": "zero tests collected"}
    if n["failures"] or n["errors"]:
        return base | {"reason": f"{n['failures']} failed, {n['errors']} errors"}
    if n["skipped"]:
        return base | {"reason": f"{n['skipped']} skipped (a skip is FAIL)"}
    if proc.returncode != 0:
        return base | {"reason": f"pytest exited {proc.returncode}"}
    return base | {"status": "PASS", "reason": f"{n['tests']} passed"}


def _checksum(results: dict) -> str:
    return hashlib.sha256(json.dumps(results, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_results(path: Path = RESULTS) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"results": {}}


def write_results(results: dict, path: Path = RESULTS) -> None:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    doc = {"generated_by": "scripts/device_accept.py (do not edit by hand)", "git_head_at_run": head, "results": results, "checksum": _checksum(results)}
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def effective(criteria: list[dict], doc: dict, root: Path = ROOT) -> dict[str, dict]:
    """The status each criterion has NOW: unrecorded -> FAIL, recorded PASS whose fingerprint moved -> STALE."""
    out = {}
    for c in criteria:
        r = doc.get("results", {}).get(c["id"])
        if r is None:
            out[c["id"]] = {"status": "FAIL", "reason": "never run", "phase": c["phase"], "title": c["title"], "label": c["label"]}
        elif r["status"] == "PASS" and r.get("fingerprint") != fingerprint(c, root):
            out[c["id"]] = r | {"status": "STALE", "reason": "covered files changed since the PASS was recorded"}
        else:
            out[c["id"]] = r
    return out


def table(criteria: list[dict], eff: dict[str, dict]) -> str:
    rows = ["| ID | Phase | Status | Label | Criterion | Reason |", "|---|---|---|---|---|---|"]
    for c in criteria:
        e = eff[c["id"]]
        rows.append(f"| {c['id']} | {c['phase']} | **{e['status']}** | {c['label']} | {c['title']} | {e.get('reason', '')} |")
    return "\n".join(rows)


def write_table(criteria: list[dict], eff: dict[str, dict], path: Path = ACCEPTANCE) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else f"# ACCEPTANCE\n\n{BEGIN}\n{END}\n"
    a, b = text.index(BEGIN) + len(BEGIN), text.index(END)
    path.write_text(text[:a] + "\n" + table(criteria, eff) + "\n" + text[b:], encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "verify", "status"])
    ap.add_argument("--phase", type=int, action="append")
    ap.add_argument("--id", action="append")
    a = ap.parse_args(argv)
    criteria = load_criteria()
    doc = read_results()
    if a.cmd == "run":
        chosen = [c for c in criteria if (not a.phase or c["phase"] in a.phase) and (not a.id or c["id"] in a.id)]
        results = dict(doc.get("results", {}))
        for c in chosen:
            r = run_criterion(c)
            results[c["id"]] = r
            print(f"{c['id']:<14} {r['status']:<5} {r['reason']}")
        write_results(results)
        doc = read_results()
        write_table(criteria, effective(criteria, doc))
        return 0 if all(results[c["id"]]["status"] == "PASS" for c in chosen) else 1
    eff = effective(criteria, doc)
    if a.cmd == "verify":
        bad = []
        if doc.get("results") and doc.get("checksum") != _checksum(doc["results"]):
            bad.append("test-results.json was edited by hand (checksum mismatch)")
        bad += [f"{i} is STALE" for i, e in eff.items() if e["status"] == "STALE"]
        if ACCEPTANCE.exists() and table(criteria, eff) not in ACCEPTANCE.read_text(encoding="utf-8"):
            bad.append("ACCEPTANCE.md table is out of date: run `python scripts/device_accept.py run` or `status`")
        for b in bad:
            print("ERROR", b)
        return 1 if bad else 0
    write_table(criteria, eff)
    print(table(criteria, eff))
    passed = sum(e["status"] == "PASS" for e in eff.values())
    print(f"\n{passed}/{len(eff)} PASS")
    return 0 if passed == len(eff) else 1


if __name__ == "__main__":
    sys.exit(main())
