"""Controller-side fetch: download each approved artifact once, verify it, cache it.

Rules enforced here, in order, for every fetch:
  1. the artifact must be in the registry (built from the approved manifest / lockfile); there is no
     fetch-by-URL entry point at all;
  2. a cache hit needs no network and no approval;
  3. free disk minus the artifact's size must stay at or above the reserve, checked BEFORE asking or starting;
  4. a fresh approval shows URL, size and sha256;
  5. the stream is held to the declared size and to the reserve; the sha256 must match or the file is deleted.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import httpx

from envrec.remote.approval import ApprovalGate, PendingAction
from envrec.contracts import Origin
from envrec.runtime.manifest import Artifact, Manifest

GIB = 2**30
SIZE_SLACK = 1.10  # declared sizes for some entries are approximate; hash is the real check


class ArtifactNotInManifest(KeyError):
    pass


class DiskReserveBreach(RuntimeError):
    pass


class ChecksumMismatch(RuntimeError):
    pass


class FetchDenied(PermissionError):
    pass


def human_size(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n < 1e9 else f"{n / 1e9:.2f} GB"


class Fetcher:
    def __init__(self, registry: dict[str, Artifact], cache_dir: Path | str, gate: ApprovalGate, *,
                 reserve_gib: float = 4.0, free_fn: Callable[[Path], int] | None = None,
                 client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=60, follow_redirects=True)) -> None:
        self.registry, self.cache, self.gate = registry, Path(cache_dir), gate
        self.reserve = int(reserve_gib * GIB)
        self.free_fn = free_fn or (lambda p: shutil.disk_usage(p).free)
        self.client_factory = client_factory
        self.network_calls = 0

    @classmethod
    def from_manifest(cls, manifest: Manifest, cache_dir: Path | str, gate: ApprovalGate, **kw) -> Fetcher:
        manifest.require_approved()
        return cls(manifest.smoke_artifacts(), cache_dir, gate, reserve_gib=manifest.disk_reserve_gib, **kw)

    @classmethod
    def from_catalog(cls, catalog, manifest: Manifest, cache_dir: Path | str, gate: ApprovalGate, **kw) -> Fetcher:
        """Registry = the generated lockfile's artifacts. The manifest must STILL be approved: un-approving it stops all fetching."""
        manifest.require_approved()
        return cls(catalog.artifacts(), cache_dir, gate, reserve_gib=manifest.disk_reserve_gib, **kw)

    def path_for(self, art: Artifact) -> Path:
        return self.cache / art.sha256 / art.file

    def cached(self, art_id: str) -> Path | None:
        art = self._get(art_id)
        p = self.path_for(art)
        return p if p.is_file() and self._sha(p) == art.sha256 else None

    def _get(self, art_id: str) -> Artifact:
        try:
            return self.registry[art_id]
        except KeyError:
            raise ArtifactNotInManifest(f"{art_id!r} is not in the approved manifest/lockfile; nothing was fetched") from None

    @staticmethod
    def _sha(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def fetch(self, art_id: str) -> Path:
        art = self._get(art_id)
        hit = self.cached(art_id)
        if hit:
            return hit
        self.cache.mkdir(parents=True, exist_ok=True)
        free = self.free_fn(self.cache)
        if free - art.bytes < self.reserve:
            raise DiskReserveBreach(
                f"fetching {art.file} ({human_size(art.bytes)}) would leave {human_size(max(free - art.bytes, 0))} free, "
                f"below the {self.reserve / GIB:.1f} GiB reserve; nothing was started")
        pending = PendingAction("fetch_artifact", f"GET {art.url}", "controller", Origin.builtin, art.sha256,
                                f"{art.file} (about {human_size(art.bytes)}, license {art.license or 'unstated'})")
        if not self.gate.request(pending):
            raise FetchDenied(f"download of {art.file} was not approved")
        dest = self.path_for(art)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".partial")
        h, got = hashlib.sha256(), 0
        cap = int(art.bytes * SIZE_SLACK) + 2_000_000
        try:
            self.network_calls += 1
            with self.client_factory() as client, client.stream("GET", art.url) as r:
                r.raise_for_status()
                with tmp.open("wb") as out:
                    for chunk in r.iter_bytes(1 << 20):
                        got += len(chunk)
                        if got > cap:
                            raise ChecksumMismatch(f"{art.file} is larger than declared ({human_size(got)} > {human_size(cap)}); aborted")
                        if self.free_fn(self.cache) - len(chunk) < self.reserve:
                            raise DiskReserveBreach("the download reached the disk reserve and was aborted")
                        h.update(chunk)
                        out.write(chunk)
            if h.hexdigest() != art.sha256:
                raise ChecksumMismatch(f"{art.file}: sha256 {h.hexdigest()[:12]}... does not match the approved {art.sha256[:12]}...")
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)  # nothing partial survives, whatever went wrong
            if dest.exists() and self._sha(dest) != art.sha256:
                dest.unlink(missing_ok=True)
            raise
        return dest

    def total_cached_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.cache.rglob("*") if p.is_file()) if self.cache.exists() else 0
