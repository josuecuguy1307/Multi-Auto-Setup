"""Read-only loaders. Nothing in this module writes either file; writing targets.yaml is enroll's job alone."""

from __future__ import annotations

from pathlib import Path

import yaml

from envrec.mission.models import MissionCard, TargetConfig


def load_mission(path: Path | str) -> MissionCard:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return MissionCard.model_validate(data)


def load_targets(path: Path | str) -> TargetConfig:
    p = Path(path)
    if not p.is_file():
        return TargetConfig()  # no file = no authorized targets
    return TargetConfig.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
