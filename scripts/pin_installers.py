"""Write a release's tag and SHA-256 into the PIN block of install.sh and install.ps1 (the value each installer checks its default download against).

    python3 scripts/pin_installers.py TAG SHA256 [--base URL] [--dir DIR]

Order of a release: (1) build the archive with scripts/make_release.py and publish it with its mas-release.json; (2) run this script with the published archive's SHA-256 and commit the two
files to the public repository's main branch. An archive cannot contain its own hash, so the copy of the installer INSIDE a release pins the release before it; that copy only serves the
uninstaller. The installer a person downloads from the repository pins the newest published release."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def pin(directory: Path, tag: str, sha: str, base: str | None) -> list[str]:
    if not TAG_RE.match(tag) or not SHA_RE.match(sha):
        raise SystemExit("bad tag or sha256 (64 lowercase hex)")
    changed = []
    sh, ps = directory / "install.sh", directory / "install.ps1"
    text = sh.read_text(encoding="utf-8")
    new = re.sub(r'^DEFAULT_TAG=".*"$', f'DEFAULT_TAG="{tag}"', text, flags=re.M)
    new = re.sub(r'^DEFAULT_SHA256=".*"$', f'DEFAULT_SHA256="{sha}"', new, flags=re.M)
    if base:
        new = re.sub(r'^DEFAULT_BASE=".*"$', f'DEFAULT_BASE="{base}"', new, flags=re.M)
    if new != text:
        sh.write_text(new, encoding="utf-8")
        changed.append(sh.name)
    text = ps.read_text(encoding="utf-8")
    new = re.sub(r"^\$DefaultTag = '.*'$", f"$DefaultTag = '{tag}'", text, flags=re.M)
    new = re.sub(r"^\$DefaultSha256 = '.*'$", f"$DefaultSha256 = '{sha}'", new, flags=re.M)
    if base:
        new = re.sub(r"^\$DefaultBase = '.*'$", f"$DefaultBase = '{base}'", new, flags=re.M)
    if new != text:
        ps.write_text(new, encoding="utf-8")  # keeps the BOM: it is part of the text read as utf-8
        changed.append(ps.name)
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("sha256")
    ap.add_argument("--base", default=None)
    ap.add_argument("--dir", default=".")
    a = ap.parse_args(argv)
    ch = pin(Path(a.dir), a.tag, a.sha256, a.base)
    print("pinned in:", ", ".join(ch) or "(already up to date)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
