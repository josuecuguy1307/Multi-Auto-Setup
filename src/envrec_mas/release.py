"""`mas update`: the install layout the installer makes, and the safe replacement of one release by another.

Layout under the program home (`MAS_HOME`, default `~/.mas`):

    releases/<tag>/        the unpacked source of one release, with its own `.venv` (an editable install of that folder) and a `RELEASE` file
    current                a one-line file with the tag the shim runs; replaced atomically
    previous               the tag that ran before (kept for one more update, then pruned)
    uv                     the path of the `uv` the installer used

A release is built next to the running one and only becomes current by replacing `current`; any failure before that deletes the new folder, any failure after it puts the old tag back.
The expected SHA-256 comes from the release's manifest (`mas-release.json`) published with the archive. That protects against a corrupt or swapped download, not against a compromised
publisher: the trust root is the public repository over HTTPS (docs/INSTALL.md says so)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import envrec_os

DEFAULT_BASE = "https://github.com/josuecuguy1307/Multi-Auto-Setup/releases"
MANIFEST_NAME = "mas-release.json"
MAX_ARCHIVE = 200 * 1024 * 1024
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


class UpdateRefused(Exception):
    def __init__(self, reason: str, text: str) -> None:
        super().__init__(text)
        self.reason, self.text = reason, text


def mas_home(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env["MAS_HOME"]).expanduser() if env.get("MAS_HOME") else Path.home() / ".mas"


def release_base(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("MAS_RELEASE_BASE") or DEFAULT_BASE).rstrip("/")


def clean_text(s: object, limit: int) -> str:
    """Text that came from the network is data: control characters out, length capped."""
    return "".join(ch for ch in str(s) if ch == "\n" or (ch.isprintable()))[:limit]


# ---- the layout --------------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Layout:
    home: Path

    @property
    def releases(self) -> Path:
        return self.home / "releases"

    def release_dir(self, tag: str) -> Path:
        return self.releases / tag

    def read(self, name: str) -> str | None:
        try:
            return (self.home / name).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def write_atomic(self, name: str, text: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.home, prefix=f".{name}-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        os.replace(tmp, self.home / name)

    @property
    def current(self) -> str | None:
        return self.read("current")

    @property
    def previous(self) -> str | None:
        return self.read("previous")

    def installed(self) -> bool:
        return self.current is not None and self.release_dir(self.current).is_dir()


def release_info(root: Path) -> dict | None:
    try:
        return json.loads((root / "RELEASE").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def running_release() -> dict | None:
    """The RELEASE record of the folder this program runs from (None for a source checkout)."""
    return release_info(Path(envrec_os.SKILLS_DIR).parent)


# ---- network -----------------------------------------------------------------------------------------------------------------------------------

def _check_url(url: str) -> None:
    u = urllib.parse.urlparse(url)
    if u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in [h.strip("[]") for h in LOOPBACK]):
        return
    raise UpdateRefused("insecure_url", f"Downloads are only made over HTTPS (or http on this same computer): {clean_text(url, 80)}")


def http_get(url: str, *, limit: int = MAX_ARCHIVE, timeout: float = 30.0) -> bytes:
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "mas-update"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _check_url(resp.geturl())  # a redirect may not leave HTTPS
            data = resp.read(limit + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdateRefused("network", f"Could not download {clean_text(url, 100)}: {clean_text(getattr(exc, 'reason', exc), 100)}") from None
    if len(data) > limit:
        raise UpdateRefused("too_large", "The download is larger than allowed; it was not used.")
    return data


Fetch = Callable[[str], bytes]


def fetch_manifest(base: str, fetch: Fetch = http_get) -> dict:
    raw = fetch(f"{base}/latest/download/{MANIFEST_NAME}")
    try:
        m = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise UpdateRefused("bad_manifest", "The latest release's manifest could not be read.") from None
    if not isinstance(m, dict) or not TAG_RE.match(str(m.get("tag", ""))) or not SHA_RE.match(str(m.get("sha256", ""))) or not re.match(r"^[A-Za-z0-9._-]{1,120}\.tar\.gz$", str(m.get("archive", ""))):
        raise UpdateRefused("bad_manifest", "The latest release's manifest does not have the expected shape (tag, archive, sha256). Nothing was updated.")
    return {"tag": m["tag"], "archive": m["archive"], "sha256": m["sha256"], "notes": clean_text(m.get("notes", ""), 1500), "size": m.get("size") if isinstance(m.get("size"), int) else None}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(data: bytes, expected: str) -> None:
    got = sha256_hex(data)
    if got != expected.lower():
        raise UpdateRefused("sha_mismatch", f"The SHA-256 does not match (expected {expected[:16]}..., obtained {got[:16]}...). The download was not used; nothing was changed.")


# ---- unpacking ---------------------------------------------------------------------------------------------------------------------------------

def safe_extract(data: bytes, dest: Path) -> None:
    """Unpack a release archive into `dest`, dropping its single top folder. Refuses absolute names, `..`, links and devices: nothing can land outside `dest`."""
    dest.mkdir(parents=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = tf.getmembers()
        tops = {m.name.split("/", 1)[0] for m in members if m.name not in (".", "")}
        if len(tops) != 1:
            raise UpdateRefused("bad_archive", "The release archive must have a single root folder.")
        for m in members:
            parts = m.name.split("/")[1:]
            if not parts or parts == [""]:
                continue
            if m.name.startswith("/") or ".." in parts or "\\" in m.name or ":" in parts[0]:
                raise UpdateRefused("bad_archive", f"The archive contains a path that is not allowed ({clean_text(m.name, 60)}). It was not used.")
            if not (m.isfile() or m.isdir()):
                raise UpdateRefused("bad_archive", f"The archive contains something that is neither a file nor a folder ({clean_text(m.name, 60)}). It was not used.")
            target = (root.joinpath(*parts)).resolve()
            if root not in target.parents and target != root:
                raise UpdateRefused("bad_archive", "The archive tries to write outside its folder. It was not used.")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                assert src is not None
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                envrec_os.set_executable(target, bool(m.mode & 0o111))


# ---- building and switching --------------------------------------------------------------------------------------------------------------------

Runner = Callable[[Sequence[str], Path | None], "tuple[int, str]"]


def _rel_python() -> Path:
    import sys
    exe = Path(sys.executable)
    try:
        return exe.relative_to(Path(sys.prefix))
    except ValueError:
        return Path("bin") / exe.name if (Path(sys.prefix) / "bin").is_dir() else Path("Scripts") / exe.name


def release_python(layout: Layout, tag: str) -> Path:
    return layout.release_dir(tag) / ".venv" / _rel_python()


def build_release(layout: Layout, tag: str, data: bytes, sha: str, *, uv: str, python_version: str, run: Runner, now: Callable[[], float] = time.time) -> Path:
    """Unpack into a `.partial` folder, make its venv, install it editable, smoke-test it, then rename it into place. Leaves no partial folder behind on failure."""
    final = layout.release_dir(tag)
    partial = layout.releases / f".{tag}.partial"
    layout.releases.mkdir(parents=True, exist_ok=True)
    for stale in (partial,):
        shutil.rmtree(stale, ignore_errors=True)
    try:
        safe_extract(data, partial)
        # the venv must be created where it will live: its scripts carry absolute paths. So build at the final path (a half-built final folder is removed on failure).
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    if final.exists():
        shutil.rmtree(partial, ignore_errors=True)
        raise UpdateRefused("exists", f"Version {tag} is already in {final}. Use the installer to repair it.")
    os.replace(partial, final)
    try:
        rc, out = run([uv, "venv", "--python", python_version, str(final / ".venv")], None)
        if rc != 0:
            raise UpdateRefused("venv_failed", f"Could not create the environment: {clean_text(out, 300)}")
        py = str(final / ".venv" / _rel_python())
        rc, out = run([uv, "pip", "install", "--python", py, "-e", str(final)], None)
        if rc != 0:
            raise UpdateRefused("install_failed", f"Could not install version {tag}: {clean_text(out, 300)}")
        (final / "RELEASE").write_text(json.dumps({"tag": tag, "sha256": sha, "installed_at": int(now())}), encoding="utf-8")
        rc, out = run([py, "-m", "envrec_mas", "--version"], None)
        if rc != 0 or tag not in out:
            raise UpdateRefused("smoke_failed", f"Version {tag} does not start ({clean_text(out, 300)}).")
    except BaseException:
        shutil.rmtree(final, ignore_errors=True)
        raise
    return final


def switch(layout: Layout, tag: str, *, smoke: Callable[[], bool]) -> None:
    """Make `tag` current; if the smoke test through the new pointer fails, put the old one back and say so."""
    old = layout.current
    layout.write_atomic("current", tag)
    if old and old != tag:
        layout.write_atomic("previous", old)
    if not smoke():
        if old:
            layout.write_atomic("current", old)
        raise UpdateRefused("switch_failed", f"Version {tag} did not respond after the switch; went back to {old or 'the previous one'}.")


def prune(layout: Layout) -> list[str]:
    """Delete releases that are neither current nor previous. Returns what was removed."""
    keep = {layout.current, layout.previous}
    gone = []
    if layout.releases.is_dir():
        for d in sorted(layout.releases.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)
                gone.append(d.name)
    return gone
