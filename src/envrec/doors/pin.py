"""LEGACY (retired by docs/NETWORK_CONTRACT.md 2.5 and section 1.5; kept until Phase 1 replaces it, NOT reachable from the `mas` commands; conformance and tests/test_mas_legacy.py check that): Pin the host key of a paired computer, only after the owner's 6 digits match the code computed from the key the Mac actually sees. A mismatch aborts and writes nothing."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from envrec.doors.pair_code import pairing_code

KNOWN_HOSTS = Path.home() / ".ssh" / "envrec_known_hosts"


class PairingRefused(Exception):
    pass


def scan_host_key(host: str, port: int = 22, *, run: Callable = subprocess.run) -> str:
    """The host's ed25519 public key line as the Mac sees it (`ssh-keyscan`). Reads nothing else from the host."""
    done = run(["ssh-keyscan", "-T", "8", "-p", str(port), "-t", "ed25519", host], capture_output=True, text=True, timeout=20)
    for ln in done.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 3 and parts[1] == "ssh-ed25519":
            return f"{parts[1]} {parts[2]}"
    raise PairingRefused(f"{host} did not offer an ed25519 host key on port {port}")


def pin_if_code_matches(host: str, typed_code: str, port: int = 22, *, known_hosts: Path = KNOWN_HOSTS, scan: Callable[[str, int], str] = scan_host_key) -> str:
    """Compare the owner's typed 6 digits with the code of the key seen now. Equal: append `host key` to the pinned file and return the code. Different: raise, write nothing."""
    typed = typed_code.strip()
    if not (typed.isdigit() and len(typed) == 6):
        raise PairingRefused("the pairing code is 6 digits")
    key = scan(host, port)
    if pairing_code(key) != typed:
        raise PairingRefused("the code does not match the host key this Mac sees: nothing was pinned and no connection was made")
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    entry = (f"[{host}]:{port}" if port != 22 else host) + f" {key}\n"
    existing = known_hosts.read_text() if known_hosts.exists() else ""
    if entry not in existing:
        with known_hosts.open("a") as fh:
            fh.write(entry)
    known_hosts.chmod(0o600)
    return typed
