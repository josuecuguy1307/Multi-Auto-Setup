"""LEGACY (retired by docs/NETWORK_CONTRACT.md 2.5 and section 1.5; kept until Phase 1 replaces it, NOT reachable from the `mas` commands; conformance and tests/test_mas_legacy.py check that): The proposal of a target found through a door: address, user, computer name and host key fingerprint, written DISABLED to proposals/. Only the interactive enrollment (the owner typing the
computer's name and yes) writes targets.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from envrec.enroll.enroll import render_yaml


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (("d-" + s) if not s or not s[0].isalpha() else s)[:32].rstrip("-") or "computer"


def write_target_proposal(directory: Path, *, address: str, user: str, computer: str, os_text: str, fingerprint: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    entry = {"id": slug(computer), "name": computer, "host": address, "os": (os_text.split()[0].lower() if os_text else None), "ssh_user": user, "host_key_fingerprint": fingerprint, "reported_os": os_text}
    path = directory / "targets.proposed.yaml"
    path.write_text("# PROPOSED by the door, from what the other computer reported. Nothing here authorizes anything: enroll it with `python -m envrec enroll-proposed` (you type its name and yes).\n"
                    + render_yaml({"proposed": [entry]}))
    return path


def read_target_proposal(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return data["proposed"][0]
