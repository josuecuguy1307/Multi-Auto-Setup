"""LEGACY (retired by docs/NETWORK_CONTRACT.md 2.5 and section 1.5; kept until Phase 1 replaces it, NOT reachable from the `mas` commands; conformance and tests/test_mas_legacy.py check that): The 6-digit pairing code: derived from the device's SSH ed25519 HOST key, so the owner reading it on the device and typing it on the Mac proves that the Mac is talking to the
machine in front of them and not to something in between. The same formula is in the Windows door file (PowerShell); tests pin both."""

from __future__ import annotations

import base64
import hashlib


def host_key_blob(pub_line: str) -> bytes:
    """The raw key bytes of an OpenSSH public key line `ssh-ed25519 AAAA... comment`."""
    parts = pub_line.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("not an ssh-ed25519 public key line")
    return base64.b64decode(parts[1], validate=True)


def pairing_code(pub_line: str) -> str:
    """First four bytes of SHA-256 of the key blob, big-endian, modulo 1,000,000, zero-padded to six digits."""
    digest = hashlib.sha256(host_key_blob(pub_line)).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"
