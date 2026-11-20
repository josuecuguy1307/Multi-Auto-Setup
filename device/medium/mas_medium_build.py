"""Builds the portable medium (D-034). Runs on the device owner's machine at provisioning, never on a host, and is not copied to the medium.

    python device/medium/mas_medium_build.py fetch [--cache runs/medium-cache]      download the pinned inputs, checking each sha256
    python device/medium/mas_medium_build.py build --out <dir> --key-pub <64 hex> [--cache ...]

Layout of a built medium (no links: a FAT/exFAT stick cannot hold them; link members of the interpreter archive become copies):

    launch.sh                     the one command (`sh <medium>/launch.sh --root ... --state ... --target ...`)
    runtime/launch.py             manifest check, host profile, then the gate
    runtime/python/               python-build-standalone CPython for linux x86_64, pruned (no tcl/tk, tests, headers, terminfo, pip)
    runtime/site/                 the vendored wheels, unpacked (cryptography, cffi, pycparser), licences included
    runtime/mas_device/*.py       the device code (the gate), byte for byte (sources only)
    runtime/mas_fleet/*.py        the fleet join (D-036), byte for byte (sources only)
    pairing.json                  the key this medium is paired with (mas_device.pairing.provision)
    MANIFEST.sha256               every other file and its sha256

Inputs are pinned in medium.lock.json and must already be in the cache with matching hashes; build never downloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_TREE = HERE.parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(DEFAULT_TREE / "src"))

from mas_device import medium, pairing  # noqa: E402

MANIFEST = medium.MANIFEST
PACKAGES = ("mas_device", "mas_fleet")  # the gate, and the fleet join that drives it (D-036); sources only
PY_VER = "3.13"
KEEP_BIN = {f"bin/python{PY_VER}"}
KEEP_LIB_TOP = {f"lib/libpython{PY_VER}.so.1.0", "lib/libpython3.so"}
PRUNE = tuple(f"lib/python{PY_VER}/{d}" for d in (
    "test/", "idlelib/", "tkinter/", "turtledemo/", "ensurepip/", "lib2to3/", "site-packages/", f"config-{PY_VER}-x86_64-linux-gnu/",
    "lib-dynload/_tkinter", "lib-dynload/_test", "lib-dynload/xxlimited", "lib-dynload/_xxtestfuzz", "lib-dynload/_ctypes_test"))


def load_lock(tree: Path) -> dict:
    return json.loads((Path(tree) / "device" / "medium" / "medium.lock.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _inputs(lock: dict, cache: Path) -> list[tuple[Path, dict]]:
    return [(cache / lock["python"]["file"], lock["python"])] + [(cache / "wheels" / w["file"], w) for w in lock["wheels"]]


def _keep(rel: str) -> bool:
    if rel.startswith("bin/"):
        return rel in KEEP_BIN
    if rel.startswith("lib/") and rel.count("/") == 1:
        return rel in KEEP_LIB_TOP
    if rel.startswith("lib/") and not rel.startswith(f"lib/python{PY_VER}/"):
        return False  # tcl/tk/itcl/thread trees, pkgconfig
    if not rel.startswith("lib/"):
        return False  # share/ (terminfo, man), include/
    return not rel.startswith(PRUNE)


def _safe(rel: str) -> str:
    parts = rel.split("/")
    if rel.startswith("/") or "\\" in rel or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"unsafe archive path {rel!r}")
    return rel


def _extract_python(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        by_name = {m.name: m for m in members}

        def resolve(m):
            for _ in range(32):
                if not (m.issym() or m.islnk()):
                    return m
                target = m.linkname if m.islnk() else posixpath.normpath(posixpath.join(posixpath.dirname(m.name), m.linkname))
                m = by_name.get(target)
                if m is None:
                    return None
            raise ValueError("link loop in the interpreter archive")

        for m in members:
            if m.name == "python":
                continue
            if not m.name.startswith("python/"):
                raise ValueError(f"unexpected member {m.name!r}")
            rel = _safe(m.name[len("python/"):].rstrip("/"))
            if not _keep(rel) or m.isdir():
                continue
            src = resolve(m)
            if src is None or not src.isfile():
                continue  # a dangling link, or a link to a folder: nothing the runtime needs
            out = dest.joinpath(*rel.split("/"))
            out.parent.mkdir(parents=True, exist_ok=True)
            with tf.extractfile(src) as f, open(out, "xb") as g:
                shutil.copyfileobj(f, g)
            os.chmod(out, 0o755 if src.mode & 0o111 else 0o644)
            os.utime(out, (src.mtime, src.mtime))


def _unpack_wheel(wheel: Path, dest: Path) -> None:
    with zipfile.ZipFile(wheel) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = _safe(info.filename)
            out = dest.joinpath(*rel.split("/"))
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as f, open(out, "xb") as g:
                shutil.copyfileobj(f, g)


def build(out: Path, *, cache: Path, key_pub: bytes, tree: Path | None = None) -> Path:
    tree = Path(tree or DEFAULT_TREE)
    lock = load_lock(tree)
    cache = Path(cache)
    for path, item in _inputs(lock, cache):
        if not path.is_file() or _sha256(path) != item["sha256"]:
            raise ValueError(f"ERROR {path.name} is missing from the cache or does not match medium.lock.json (run fetch)")
    out = Path(out)
    out.mkdir(parents=True)  # FileExistsError: a medium is never built over another one
    runtime = out / "runtime"
    _extract_python(cache / lock["python"]["file"], runtime / "python")
    for w in lock["wheels"]:
        _unpack_wheel(cache / "wheels" / w["file"], runtime / "site")
    for pkg in PACKAGES:
        (runtime / pkg).mkdir()
        for f in sorted((tree / "src" / pkg).glob("*.py")):
            (runtime / pkg / f.name).write_bytes(f.read_bytes())
    sh = (tree / "device" / "medium" / "launch.sh").read_bytes()
    if b"\r" in sh:
        raise ValueError("ERROR launch.sh has CR line endings (see .gitattributes: *.sh eol=lf)")
    (out / "launch.sh").write_bytes(sh)
    (runtime / "launch.py").write_bytes((tree / "device" / "medium" / "launch.py").read_bytes())
    pairing.provision(out, key_pub)
    hashes, problems = medium.hash_tree(out)
    if problems:
        raise ValueError("ERROR the built medium is not plain files: " + "; ".join(problems[:5]))
    (out / MANIFEST).write_bytes(medium.manifest_bytes(hashes))
    return out


def fetch(cache: Path, tree: Path | None = None) -> None:
    lock = load_lock(tree or DEFAULT_TREE)
    for path, item in _inputs(lock, Path(cache)):
        if path.is_file() and _sha256(path) == item["sha256"]:
            print(f"ok      {path.name}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        with urllib.request.urlopen(item["url"], timeout=300) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        if _sha256(tmp) != item["sha256"]:
            tmp.unlink()
            raise SystemExit(f"ERROR sha256 mismatch for {path.name}")
        os.replace(tmp, path)
        print(f"fetched {path.name}")
    print(f"then, with Docker running: docker pull {lock['base_image']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mas_medium_build")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--cache", default=str(DEFAULT_TREE / "runs" / "medium-cache"))
    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    b.add_argument("--key-pub", required=True, help="the paired key's Ed25519 public key, 64 hex digits")
    b.add_argument("--cache", default=str(DEFAULT_TREE / "runs" / "medium-cache"))
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        fetch(Path(a.cache))
    else:
        print(build(Path(a.out), cache=Path(a.cache), key_pub=bytes.fromhex(a.key_pub)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
