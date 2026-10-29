"""The human-approved runtime manifest. Everything downloaded must trace back to it.

`approved: false` makes every network-touching entry point refuse. The flag is the human's: no code here sets it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ManifestError(ValueError):
    pass


class NotApproved(PermissionError):
    """The manifest says approved: false. Nothing is generated or fetched until the human flips it."""


@dataclass(frozen=True)
class Artifact:
    id: str
    kind: str  # runtime | model
    file: str
    url: str
    sha256: str
    bytes: int  # declared size; the download is held to it (small tolerance) while streaming
    license: str = ""
    license_source: str = ""
    source: str = ""  # runtime project or model publisher id
    meta: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)


@dataclass
class Manifest:
    approved: bool
    disk_reserve_gib: float
    license_allowlist: list[str]
    raw: dict[str, Any]
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | str) -> Manifest:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for key in ("approved", "disk_reserve_gib", "runtime_sources", "model_sources"):
            if key not in raw:
                raise ManifestError(f"manifest is missing {key!r}")
        return cls(bool(raw["approved"]) is True and raw["approved"] is True, float(raw["disk_reserve_gib"]),
                   list(raw.get("license_allowlist", [])), raw, Path(path))

    def require_approved(self) -> None:
        if not self.approved:
            raise NotApproved("config/runtime_manifest.yaml says approved: false; the human must set it to true")

    def approved_runtime_ids(self) -> list[str]:
        return [s["id"] for s in self.raw["runtime_sources"] if s.get("status") == "approve"]

    def approved_model_source_ids(self) -> list[str]:
        return [s["id"] for s in self.raw["model_sources"] if s.get("status") == "approve"]

    def smoke_artifacts(self) -> dict[str, Artifact]:
        """The cross-checked initial set (inside the approved sources), as fetchable artifacts keyed by id."""
        smoke = self.raw.get("smoke_set") or {}
        out: dict[str, Artifact] = {}
        rt = smoke.get("runtime") or {}
        src = next((x for x in self.raw["runtime_sources"] if x["id"] == rt.get("source")), None)
        if src is None:
            raise ManifestError(f"smoke_set names runtime source {rt.get('source')!r} which is not in runtime_sources")
        for platform, a in (smoke.get("runtime_assets") or {}).items():
            aid = f"{rt['source']}:{rt['tag']}:{platform}:cpu"
            out[aid] = Artifact(aid, "runtime", a["file"], a["url"], a["sha256"], int(a["bytes"]), src["license"],
                                src["license_source"], rt["source"],
                                {"platform": platform, "tag": rt["tag"], "backend": "cpu", "server_binary": src.get("server_binary", "")})
        for m in smoke.get("models") or []:
            out[m["id"]] = Artifact(m["id"], "model", m["file"], m["url"], m["sha256"], int(m["bytes"]), m["license"],
                                    m.get("license_source", ""), m["source"],
                                    {"tier": m.get("tier"), "min_ram_gib": m.get("min_ram_gib"), "context": m.get("context")})
        return out
