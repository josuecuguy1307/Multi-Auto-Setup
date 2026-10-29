"""Model profiles come from a YAML file. No model name appears in code."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from envrec.contracts import ModelKind, ModelProfile
from envrec.model.base import ModelAdapter
from envrec.model.claude_cli import ClaudeCliModel
from envrec.model.openai_compat import OpenAICompatModel


def load_profile(path: Path | str, name: str, include_local: bool = True) -> tuple[ModelProfile, dict[str, Any]]:
    """Return the profile and its adapter options (timeouts, json_mode, extra_body, ...).

    A file `<name>.local.yaml` next to the config (same shape, kept out of version control) overrides profiles by name and adds new ones. `include_local=False` reads only the shipped file.
    """
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    profiles = dict((data or {}).get("profiles", {}))
    local = path.with_name(f"{path.stem}.local{path.suffix}")
    if include_local and local.is_file():
        profiles.update((yaml.safe_load(local.read_text(encoding="utf-8")) or {}).get("profiles", {}))
    if name not in profiles:
        raise KeyError(f"model profile {name!r} not found in {path}; have: {sorted(profiles)}")
    entry = dict(profiles[name])
    options = entry.pop("options", {}) or {}
    return ModelProfile.model_validate(entry), options


def make_adapter(profile: ModelProfile, options: dict[str, Any] | None = None) -> ModelAdapter:
    options = dict(options or {})
    if profile.kind == ModelKind.openai_compat:
        return OpenAICompatModel(profile, **options)
    if profile.kind == ModelKind.claude_cli:
        return ClaudeCliModel(profile, **options)
    raise ValueError(f"profile kind {profile.kind.value!r} needs to be built in code (scripted models)")


def with_view_budget(profile: ModelProfile, view_budget_facts: int | None, menu_max: int | None) -> ModelProfile:
    """The dials that separate a small profile from a strong one. Nothing else changes: the protocols are the same for every model."""
    update: dict = {}
    if view_budget_facts is not None:
        update["view_budget_facts"] = view_budget_facts
    if menu_max is not None:
        update["menu_max"] = menu_max
    return ModelProfile.model_validate({**profile.model_dump(), **update})
