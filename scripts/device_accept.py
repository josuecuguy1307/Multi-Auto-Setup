"""The acceptance oracle for the MAS portable device: the only thing that may turn a criterion to PASS.

    python scripts/device_accept.py run [--phase N] [--id ID ...]   run criteria, write test-results.json and the ACCEPTANCE.md table
    python scripts/device_accept.py verify                          exit 1 if a recorded PASS is stale, the results were hand-edited or the table is out of date
    python scripts/device_accept.py status                          print the table (writes nothing); exit 0 only when every criterion is PASS
    python scripts/device_accept.py confirm                         clean tree only: re-run EVERY criterion in a fresh git worktree at HEAD,
                                                                    ignoring recorded results; exit 0 only when all PASS (the loop's stop condition)

Default verdict is FAIL. A criterion is PASS only when every test it lists exists, was collected, ran, and passed: a skip, an xfail, an error, a
missing file, zero tests, fewer than `min_tests`, a timeout, or a saboteur that survives is FAIL.

Hermetic runs (reviewer R-1 M3): pytest runs with the oracle's own `device/acceptance/pytest.ini` (no addopts), `--rootdir` at the root under
test, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, every `PYTEST_*` variable removed, and a refusal when any conftest.py sits on a test's path.
A PASS records a fingerprint over the covered files, the test files, the canonical criterion itself and the oracle's own identity (this file
and its ini); generated blocks (BEGIN/END GENERATED) are left out of the hash, so rewriting the ACCEPTANCE.md table never stales anything.

Saboteurs (R-1 M4): a criterion may list `saboteurs: [{name, file, old, new}]`. Each is applied to a copy of the tree and the criterion's
tests are run there; at least one test that passed on the real tree must fail, or the criterion is FAIL ("survived"). A missing anchor is FAIL.
Output is short on purpose; full pytest output goes to runs/acceptance/<id>.log (search it for ERROR / FAILED).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRITERIA = ROOT / "device" / "acceptance" / "criteria.yaml"
RESULTS = ROOT / "test-results.json"
ACCEPTANCE = ROOT / "ACCEPTANCE.md"
ORACLE_INI = Path(__file__).resolve().parents[1] / "device" / "acceptance" / "pytest.ini"
BEGIN, END = "<!-- BEGIN GENERATED: device_accept.py -->", "<!-- END GENERATED: device_accept.py -->"
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".venv", "runs", ".git"}
DEFAULT_TIMEOUT_S = 3600


def load_criteria(path: Path | None = None) -> list[dict]:
    import yaml
    doc = yaml.safe_load(Path(path or CRITERIA).read_text(encoding="utf-8"))
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
    """The files a covered path stands for. Skipped folders are pruned while walking (not filtered afterwards: runs/ and .venv hold tens
    of thousands of files), and the results file this oracle writes is never part of what it judges (a criterion covering "." would
    otherwise go stale the moment its own result is recorded)."""
    p = root / rel.split("::", 1)[0]
    if p.is_file():
        return [p]
    if not p.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_PARTS)
        for name in filenames:
            f = Path(dirpath, name)
            rp = f.relative_to(root)
            if f.is_file() and rp.as_posix() != RESULTS.name and not (SKIP_PARTS & set(rp.parts)):
                out.append(f)
    return sorted(out)


def _normalised(data: bytes) -> bytes:
    """CRLF-insensitive, and without generated blocks: rewriting the oracle's own table is not a change to what was judged."""
    data = data.replace(b"\r\n", b"\n")
    b, e = BEGIN.encode(), END.encode()
    while b in data and e in data and data.index(b) < data.index(e):
        i, j = data.index(b), data.index(e) + len(e)
        data = data[:i] + b"<generated>" + data[j:]
    return data


def oracle_identity() -> str:
    """What judged the result: this file and its pytest configuration."""
    parts = []
    for p in (Path(__file__).resolve(), ORACLE_INI):
        data = p.read_bytes().replace(b"\r\n", b"\n") if p.exists() else b"<missing>"
        parts.append(hashlib.sha256(data).hexdigest())
    return ":".join(parts)


def fingerprint(c: dict, root: Path = ROOT) -> str:
    """sha256 over the canonical criterion, the oracle's identity, and (path, content hash) of every covered and test file."""
    h = hashlib.sha256()
    h.update(json.dumps(c, sort_keys=True, separators=(",", ":"), default=str).encode() + b"\n")
    h.update(oracle_identity().encode() + b"\n")
    for rel in sorted(set(c["covers"]) | {t.split("::", 1)[0] for t in c["tests"]}):
        files = _files(rel, root)
        if not files:
            h.update(f"{rel}\0<missing>\n".encode())
        for f in files:
            h.update(f"{f.relative_to(root).as_posix()}\0{hashlib.sha256(_normalised(f.read_bytes())).hexdigest()}\n".encode())
    return h.hexdigest()


def _junit(xml_path: Path) -> tuple[dict, set[str], set[str]]:
    tree = ET.parse(xml_path)
    agg = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    suites = [tree.getroot()] if tree.getroot().tag == "testsuite" else tree.getroot().findall("testsuite")
    passed, failed = set(), set()
    for s in suites:
        for k in agg:
            agg[k] += int(s.get(k, 0))
        for case in s.iter("testcase"):
            node = f"{case.get('classname', '')}::{case.get('name', '')}"
            bad = [ch for ch in case if ch.tag in ("failure", "error", "skipped")]
            (failed if bad else passed).add(node)
    return agg, passed, failed


def _conftests(c: dict, root: Path) -> list[str]:
    """Every conftest.py pytest could load for these tests: in each test's folder and its parents up to the root."""
    found = set()
    for t in c["tests"]:
        d = (root / t.split("::", 1)[0]).parent
        while True:
            if (d / "conftest.py").exists():
                found.add((d / "conftest.py").relative_to(root).as_posix())
            if d == root or root not in d.parents:
                break
            d = d.parent
    return sorted(found)


def _pytest(c: dict, root: Path, xml: Path, log_path: Path, python: str) -> tuple[int | None, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != "PYTHONPATH"}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(Path(root) / "src"))  # the tree under test first
    cmd = [python, "-m", "pytest", "-c", str(ORACLE_INI), f"--rootdir={root}", "-q", "-p", "no:cacheprovider", "-rfEs",
           f"--junitxml={xml}", *c["tests"]]
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(cmd, cwd=root, stdout=log, stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL,
                                  timeout=float(c.get("timeout_s", DEFAULT_TIMEOUT_S)))
        return proc.returncode, ""
    except subprocess.TimeoutExpired:
        return None, f"timeout after {c.get('timeout_s', DEFAULT_TIMEOUT_S)} s"


def _copy_tree(root: Path, dst: Path) -> None:
    shutil.copytree(root, dst, ignore=shutil.ignore_patterns(*SKIP_PARTS, "node_modules"), symlinks=True)


def _saboteurs(c: dict, root: Path, passed: set[str], python: str) -> tuple[dict, str]:
    out = {}
    for s in c.get("saboteurs", []):
        name = s["name"]
        target = root / s["file"]
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        if text.count(s["old"]) != 1:
            return out | {name: "anchor missing"}, f"saboteur {name}: anchor not found exactly once in {s['file']}"
        with tempfile.TemporaryDirectory(prefix="sab-") as tmp:
            copy = Path(tmp) / "tree"
            _copy_tree(root, copy)
            (copy / s["file"]).write_text(text.replace(s["old"], s["new"]), encoding="utf-8", newline="")
            xml = Path(tmp) / "sab.xml"
            code, why = _pytest(c, copy, xml, Path(tmp) / "sab.log", python)
            if xml.exists():
                _, _, failed = _junit(xml)
                caught = bool(passed & failed)
            else:
                caught = code not in (0, None) or bool(why)  # a weakened build that no longer even collects is caught
        out[name] = "killed" if caught else "survived"
        if not caught:
            return out, f"saboteur {name} survived: no test that passes on the real tree failed"
    return out, ""


def run_criterion(c: dict, root: Path = ROOT, python: str = sys.executable) -> dict:
    root = Path(root)
    fp = fingerprint(c, root)
    base = {"status": "FAIL", "phase": c["phase"], "title": c["title"], "label": c["label"], "fingerprint": fp, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    missing = [t for t in c["tests"] if not (root / t.split("::", 1)[0]).exists()]
    if not c["tests"]:
        return base | {"reason": "no tests listed"}
    if missing:
        return base | {"reason": "missing test files: " + ", ".join(missing)}
    conf = _conftests(c, root)
    if conf:
        return base | {"reason": "a conftest.py on the test path can change what runs: " + ", ".join(conf)}
    logs = root / "runs" / "acceptance"
    logs.mkdir(parents=True, exist_ok=True)
    xml = logs / f"{c['id']}.xml"
    xml.unlink(missing_ok=True)
    code, why = _pytest(c, root, xml, logs / f"{c['id']}.log", python)
    if why:
        return base | {"reason": why}
    if not xml.exists():
        return base | {"reason": f"pytest exited {code} without a report (see runs/acceptance/{c['id']}.log)"}
    n, passed, _ = _junit(xml)
    base["counts"] = n
    result = None
    if n["tests"] == 0:
        result = base | {"reason": "zero tests collected"}
    elif n["failures"] or n["errors"]:
        result = base | {"reason": f"{n['failures']} failed, {n['errors']} errors"}
    elif n["skipped"]:
        result = base | {"reason": f"{n['skipped']} skipped (a skip or xfail is FAIL)"}
    elif code != 0:
        result = base | {"reason": f"pytest exited {code}"}
    elif n["tests"] < int(c.get("min_tests", 1)):
        result = base | {"reason": f"{n['tests']} tests, fewer than min_tests {c['min_tests']}"}
    if c.get("saboteurs"):
        sab, bad = _saboteurs(c, root, passed, python)
        base["saboteurs"] = sab
        if result is None and bad:
            result = base | {"reason": bad}
        elif result is not None:
            result["saboteurs"] = sab
    if result is not None:
        return result
    return base | {"status": "PASS", "reason": f"{n['tests']} passed" + (f", {len(c['saboteurs'])} saboteurs killed" if c.get("saboteurs") else "")}


def _checksum(results: dict) -> str:
    return hashlib.sha256(json.dumps(results, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_results(path: Path | None = None) -> dict:
    try:
        return json.loads(Path(path or RESULTS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"results": {}}


def write_results(results: dict, path: Path | None = None, root: Path = ROOT) -> None:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    doc = {"generated_by": "scripts/device_accept.py (do not edit by hand)", "git_head_at_run": head, "results": results, "checksum": _checksum(results)}
    Path(path or RESULTS).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def effective(criteria: list[dict], doc: dict, root: Path = ROOT) -> dict[str, dict]:
    """The status each criterion has NOW: unrecorded -> FAIL, recorded PASS whose fingerprint moved -> STALE."""
    out = {}
    for c in criteria:
        r = doc.get("results", {}).get(c["id"])
        if r is None:
            out[c["id"]] = {"status": "FAIL", "reason": "never run", "phase": c["phase"], "title": c["title"], "label": c["label"]}
        elif r["status"] == "PASS" and r.get("fingerprint") != fingerprint(c, root):
            out[c["id"]] = r | {"status": "STALE", "reason": "covered files, the criterion or the oracle changed since the PASS was recorded"}
        else:
            out[c["id"]] = r
    return out


def table(criteria: list[dict], eff: dict[str, dict]) -> str:
    rows = ["| ID | Phase | Status | Label | Criterion | Reason |", "|---|---|---|---|---|---|"]
    for c in criteria:
        e = eff[c["id"]]
        rows.append(f"| {c['id']} | {c['phase']} | **{e['status']}** | {c['label']} | {c['title']} | {e.get('reason', '')} |")
    return "\n".join(rows)


def write_table(criteria: list[dict], eff: dict[str, dict], path: Path | None = None) -> None:
    path = Path(path or ACCEPTANCE)
    text = path.read_text(encoding="utf-8") if path.exists() else f"# ACCEPTANCE\n\n{BEGIN}\n{END}\n"
    a, b = text.index(BEGIN) + len(BEGIN), text.index(END)
    path.write_text(text[:a] + "\n" + table(criteria, eff) + "\n" + text[b:], encoding="utf-8")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def confirm(root: Path = ROOT, python: str = sys.executable) -> dict:
    """R-1 M2: a clean tree, a fresh worktree at HEAD, every criterion re-run there; recorded results are never read."""
    root = Path(root)
    dirty = _git(root, "status", "--porcelain").stdout.strip()
    if dirty:
        return {"ok": False, "reason": "dirty tree: commit or stash first", "results": {}, "head": None}
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="confirm-"))
    wt = tmp / "wt"
    try:
        r = _git(root, "worktree", "add", "--detach", str(wt), head)
        if r.returncode != 0:
            return {"ok": False, "reason": "git worktree failed: " + r.stderr[-300:], "results": {}, "head": head}
        criteria = load_criteria(wt / "device" / "acceptance" / "criteria.yaml")
        results = {c["id"]: run_criterion(c, wt, python) for c in criteria}
        ok = all(r["status"] == "PASS" for r in results.values())
        return {"ok": ok, "reason": "" if ok else "not every criterion passes at HEAD", "results": results, "head": head}
    finally:
        _git(root, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "verify", "status", "confirm"])
    ap.add_argument("--phase", type=int, action="append")
    ap.add_argument("--id", action="append")
    a = ap.parse_args(argv)
    if a.cmd == "confirm":
        out = confirm(ROOT)
        for cid, r in out["results"].items():
            print(f"{cid:<14} {r['status']:<5} {r['reason']}")
        (ROOT / "runs").mkdir(exist_ok=True)
        (ROOT / "runs" / f"confirm-{(out['head'] or 'none')[:12]}.json").write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
        print(("CONFIRMED " if out["ok"] else "NOT CONFIRMED ") + (out["head"] or "") + (f" ({out['reason']})" if out["reason"] else ""))
        return 0 if out["ok"] else 1
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
        if Path(ACCEPTANCE).exists() and table(criteria, eff) not in Path(ACCEPTANCE).read_text(encoding="utf-8"):
            bad.append("ACCEPTANCE.md table is out of date: run `python scripts/device_accept.py run`")
        for b in bad:
            print("ERROR", b)
        return 1 if bad else 0
    print(table(criteria, eff))  # status: read-only
    passed = sum(e["status"] == "PASS" for e in eff.values())
    print(f"\n{passed}/{len(eff)} PASS")
    return 0 if passed == len(eff) else 1


if __name__ == "__main__":
    sys.exit(main())
