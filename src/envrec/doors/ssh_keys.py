"""LEGACY (retired by docs/NETWORK_CONTRACT.md 2.5 and section 1.5; kept until Phase 1 replaces it, NOT reachable from the `mas` commands; conformance and tests/test_mas_legacy.py check that): The Mac's own key for one paired computer: an ed25519 pair under ~/.ssh with mode 600. Only the PUBLIC half is ever returned or shown; the private path is referenced by an
environment variable NAME in targets.yaml and never printed."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

SSH_DIR = Path.home() / ".ssh"


def key_path(target_id: str, ssh_dir: Path = SSH_DIR) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9\-]{0,31}", target_id):
        raise ValueError("a target id is lowercase letters, digits and dashes")
    return ssh_dir / f"envrec_{target_id}_ed25519"


def keygen_command(target_id: str, ssh_dir: Path = SSH_DIR) -> list[str]:
    """The exact command the owner is shown before the key is made."""
    return ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"envrec-{target_id}", "-f", str(key_path(target_id, ssh_dir))]


def create_keypair(target_id: str, ssh_dir: Path = SSH_DIR, *, run: Callable = subprocess.run) -> str:
    """Make the pair (never overwriting one), set modes 700/600, return the PUBLIC key line."""
    path = key_path(target_id, ssh_dir)
    if path.exists():
        raise FileExistsError(f"{path.name} already exists; it is not overwritten")
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    done = run(keygen_command(target_id, ssh_dir), capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise RuntimeError("ssh-keygen failed: " + (done.stderr or "").strip().splitlines()[-1][:120] if (done.stderr or "").strip() else "ssh-keygen failed")
    os.chmod(path, 0o600)
    return path.with_name(path.name + ".pub").read_text().strip()
