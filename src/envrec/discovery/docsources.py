"""The official documentation hosts, read from ONE data file (config/doc_sources.yaml). The code holds no host list."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

FILE = Path(__file__).resolve().parents[3] / "config" / "doc_sources.yaml"


@dataclass(frozen=True)
class DocSources:
    hosts: frozenset[str]
    max_bytes: int
    timeout_s: float


def load_sources(path: Path = FILE) -> DocSources:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return DocSources(frozenset(str(h).lower() for h in raw.get("hosts", [])), int(raw.get("max_bytes", 400000)), float(raw.get("timeout_s", 20)))
