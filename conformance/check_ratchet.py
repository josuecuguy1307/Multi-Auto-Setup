#!/usr/bin/env python3
"""The conformance ratchet: run `pytest conformance`, compare the failing check IDs with conformance/ratchet.json, exit non-zero if a failing ID is not in the list.

A commit may remove IDs from the list and may never add one. Also prints the summary line, which every commit message body must carry.

  python conformance/check_ratchet.py            # check
  python conformance/check_ratchet.py --list     # print the failing IDs (to build the list the first time)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RATCHET = ROOT / "conformance" / "ratchet.json"


def run_conformance() -> tuple[list[str], str]:
    with tempfile.TemporaryDirectory() as d:
        xml = Path(d) / "r.xml"
        py = ROOT / ".venv" / "bin" / "python"
        done = subprocess.run([str(py if py.exists() else sys.executable), "-m", "pytest", "conformance", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"],
                              cwd=ROOT, capture_output=True, text=True)
        summary = next((ln for ln in reversed(done.stdout.splitlines()) if " passed" in ln or " failed" in ln or "error" in ln), "no summary line")
        if not xml.exists():
            return ["<pytest produced no report>"], summary
        failing = []
        for case in ET.parse(xml).getroot().iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failing.append(f"{Path(case.get('classname', '').replace('.', '/')).name}::{case.get('name')}")
    return sorted(failing), summary.strip("= ")


def main(argv: list[str]) -> int:
    failing, summary = run_conformance()
    if "--list" in argv:
        print("\n".join(failing))
        return 0
    allowed = json.loads(RATCHET.read_text())["allowed_to_fail"] if RATCHET.exists() else {}
    ids = set(allowed) if isinstance(allowed, (dict, list)) else set()
    new = [f for f in failing if f not in ids]
    fixed = sorted(ids - set(failing))
    print(f"conformance: {summary}")
    if fixed:
        print(f"{len(fixed)} listed ID(s) now pass: remove them from conformance/ratchet.json in this commit:\n  " + "\n  ".join(fixed))
    if new:
        print(f"RATCHET BROKEN: {len(new)} failing check(s) are not in conformance/ratchet.json (a commit may never add one):\n  " + "\n  ".join(new), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
