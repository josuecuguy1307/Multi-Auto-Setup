"""The catalog lockfile: what the generator found, normalized, plus the combinations computed from it.

Stored as two tables (runtime assets, model files) and a list of compatible pairs, so thousands of combinations
stay small. At run time ONLY this lockfile is used: nothing here, or anywhere downstream, reads a source again.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import Field

from envrec.contracts.base import Strict
from envrec.runtime.manifest import Artifact


class RuntimeAsset(Strict):
    id: str  # e.g. "<source>:<tag>:linux/arm64:cpu"
    source: str  # runtime project id
    tag: str
    commit: str | None = None
    os: str
    arch: str  # arm64 | amd64
    backend: str  # cpu | vulkan | cuda | metal
    file: str
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0)
    license: str
    license_source: str
    formats: list[str]
    server_binary: str
    runnable: bool  # the worker has a verified flag set for this runtime kind


class ModelFile(Strict):
    id: str  # "<repo>/<file>"
    source: str  # publisher id
    repo: str
    file: str
    url: str
    revision: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0)
    license: str
    license_source: str
    format: str  # gguf
    quantization: str
    params_b: float
    min_ram_gib: float


class CatalogEntry(Strict):
    """One runtime x backend x model x quantization x size combination, joined from the two tables."""

    id: str
    runtime: RuntimeAsset
    model: ModelFile
    min_ram_gib: float
    min_disk_gb: float


class Lock(Strict):
    version: int = 1
    manifest_sha256: str
    counts: dict[str, int] = Field(default_factory=dict)
    runtime_assets: list[RuntimeAsset] = Field(default_factory=list)
    model_files: list[ModelFile] = Field(default_factory=list)
    pairs: list[tuple[str, str]] = Field(default_factory=list)  # (runtime asset id, model file id), computed from the matrix
    excluded: list[dict[str, str]] = Field(default_factory=list)


class Catalog:
    def __init__(self, lock: Lock) -> None:
        self.lock = lock
        self._rt = {a.id: a for a in lock.runtime_assets}
        self._md = {m.id: m for m in lock.model_files}

    @classmethod
    def load(cls, path: Path | str) -> Catalog:
        return cls(Lock.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8"))))

    def entries(self):
        for rid, mid in self.lock.pairs:
            rt, md = self._rt[rid], self._md[mid]
            yield CatalogEntry(id=f"{rid}+{mid}", runtime=rt, model=md, min_ram_gib=md.min_ram_gib,
                               min_disk_gb=round((rt.bytes + md.bytes) / 1e9 * 1.1, 3))

    def __len__(self) -> int:
        return len(self.lock.pairs)

    def get(self, entry_id: str) -> CatalogEntry | None:
        rid, _, mid = entry_id.partition("+")
        if (rid, mid) not in set(self.lock.pairs) or rid not in self._rt or mid not in self._md:
            return None
        rt, md = self._rt[rid], self._md[mid]
        return CatalogEntry(id=entry_id, runtime=rt, model=md, min_ram_gib=md.min_ram_gib,
                            min_disk_gb=round((rt.bytes + md.bytes) / 1e9 * 1.1, 3))

    def artifacts(self) -> dict[str, Artifact]:
        """The only things the fetcher may download, from the lockfile and nowhere else."""
        out: dict[str, Artifact] = {}
        for a in self.lock.runtime_assets:
            out[a.id] = Artifact(a.id, "runtime", a.file, a.url, a.sha256, a.bytes, a.license, a.license_source, a.source,
                                 {"platform": f"{a.os}/{a.arch}", "backend": a.backend, "tag": a.tag, "server_binary": a.server_binary})
        for m in self.lock.model_files:
            out[m.id] = Artifact(m.id, "model", m.file, m.url, m.sha256, m.bytes, m.license, m.license_source, m.source,
                                 {"quantization": m.quantization, "params_b": m.params_b, "min_ram_gib": m.min_ram_gib})
        return out


_PARAMS = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")
_QUANT = re.compile(r"(q\d(?:_[0-9a-z]+)+|q\d_\d|q8_0)", re.I)


def parse_params_b(name: str) -> float | None:
    """a name like "Model-1.5B-Instruct" -> 1.5. First match wins; an MoE name like '30B-A3B' reads as 30 (conservative)."""
    m = _PARAMS.search(name.replace("_", "-"))
    return float(m.group(1)) if m else None


def parse_quant(filename: str) -> str | None:
    m = _QUANT.search(filename)
    return m.group(1).upper() if m else None
