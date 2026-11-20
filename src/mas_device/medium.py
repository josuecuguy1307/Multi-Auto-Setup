"""The portable medium's manifest (D-034): every file on the medium and its sha256, checked by the launcher before anything runs.

MANIFEST.sha256 holds one "<sha256>  <path>" line per file (paths relative to the medium, "/"-separated, sorted). Links are refused: a
FAT/exFAT stick cannot hold them and a link could point the launcher outside the medium. The builder (device/medium/mas_medium_build.py)
writes the manifest; this module only reads and checks, so it stays inside the device's import allowlist.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

MANIFEST = "MANIFEST.sha256"
_CHUNK = 1 << 20


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(root: Path) -> tuple[dict[str, str], list[str]]:
    """(hashes of every regular file except the manifest, problems such as links or special files)."""
    root = Path(root)
    hashes, problems = {}, []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for n in list(dirnames):
            if os.path.islink(os.path.join(dirpath, n)):
                problems.append("link: " + Path(dirpath, n).relative_to(root).as_posix())
                dirnames.remove(n)
        for n in sorted(filenames):
            p = Path(dirpath, n)
            rel = p.relative_to(root).as_posix()
            if rel == MANIFEST:
                continue
            if p.is_symlink() or not p.is_file():
                problems.append("not a regular file: " + rel)
                continue
            hashes[rel] = _sha256(p)
    return hashes, problems


def manifest_bytes(hashes: dict[str, str]) -> bytes:
    return "".join(f"{hashes[rel]}  {rel}\n" for rel in sorted(hashes)).encode("utf-8")


def read_manifest(root: Path) -> dict[str, str]:
    out = {}
    with open(Path(root) / MANIFEST, "rb") as f:
        text = f.read().decode("utf-8")
    for line in text.split("\n")[:-1]:
        digest, sep, rel = line.partition("  ")
        if not sep or len(digest) != 64 or rel in out:
            raise ValueError("malformed manifest line")
        out[rel] = digest
    return out


def verify(root: Path) -> list[str]:
    """Every problem found, or [] when the medium holds exactly the files of its manifest, byte for byte."""
    try:
        want = read_manifest(root)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return [f"manifest unreadable ({type(exc).__name__})"]
    have, problems = hash_tree(root)
    problems += ["missing: " + r for r in sorted(set(want) - set(have))]
    problems += ["not in manifest: " + r for r in sorted(set(have) - set(want))]
    problems += ["changed: " + r for r in sorted(set(want) & set(have)) if want[r] != have[r]]
    return problems
