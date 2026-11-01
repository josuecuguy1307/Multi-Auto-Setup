"""Pack a source tree into the two files a public release carries, deterministically (sorted names, fixed times and owners, no gzip timestamp), so the same tree always gives the same SHA-256:

    mas-<tag>.tar.gz     one top folder `multi-auto-setup-<tag>/` with the tree inside
    mas-release.json     {"tag", "archive", "sha256", "size", "notes"}: what `mas update` reads, and the value install.sh / install.ps1 embed for their default tag

    python3 scripts/make_release.py SOURCE_DIR TAG OUT_DIR [--notes FILE]

Run it on the clean export (~/mas-export), never on the local repository. Prints the SHA-256 to paste into the installers (`scripts/pin_installers.py` does it)."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache", "node_modules"}


def build(source: Path, tag: str) -> bytes:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", tag):
        raise SystemExit(f"bad tag {tag!r}")
    top = f"multi-auto-setup-{tag}"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for path in sorted(source.rglob("*")):
            rel = path.relative_to(source)
            if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts) or path.is_symlink():
                continue
            info = tarfile.TarInfo(f"{top}/{rel.as_posix()}" + ("/" if path.is_dir() else ""))
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            if path.is_dir():
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(info)
            elif path.is_file():
                data = path.read_bytes()
                info.size, info.mode = len(data), 0o755 if path.stat().st_mode & 0o111 else 0o644
                tf.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("tag")
    ap.add_argument("out")
    ap.add_argument("--notes", default=None)
    a = ap.parse_args(argv)
    data = build(Path(a.source).expanduser(), a.tag)
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    name = f"mas-{a.tag}.tar.gz"
    (out / name).write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    notes = Path(a.notes).read_text(encoding="utf-8") if a.notes else ""
    (out / "mas-release.json").write_text(json.dumps({"tag": a.tag, "archive": name, "sha256": sha, "size": len(data), "notes": notes}, indent=1), encoding="utf-8")
    print(f"{name}  {len(data)} bytes  sha256 {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
