"""config/discovery.yaml: the human's settings for discovery. Missing file means the safe defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT = Path("config/discovery.yaml")


@dataclass(frozen=True)
class DiscoveryConfig:
    rea_roots: tuple[Path, ...] = ()
    subnet_cap_prefix: int = 24
    scan_rate_per_s: int = 50
    ble_scan_seconds: float = 8.0
    mdns_browse_seconds: float = 6.0

    @classmethod
    def load(cls, path: Path | str | None = None) -> DiscoveryConfig:
        f = Path(path) if path else DEFAULT
        raw = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.is_file() else {}
        cap = int(raw.get("subnet_cap_prefix", 24))
        if not 22 <= cap <= 30:
            raise ValueError("subnet_cap_prefix must be between 22 and 30 (the widest allowed scan is a /22)")
        return cls(tuple(Path(p).expanduser().resolve() for p in raw.get("rea_roots") or []), cap, max(1, min(int(raw.get("scan_rate_per_s", 50)), 500)),
                   float(raw.get("ble_scan_seconds", 8)), float(raw.get("mdns_browse_seconds", 6)))
