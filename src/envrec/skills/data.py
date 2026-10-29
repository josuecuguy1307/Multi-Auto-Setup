"""Data that lives in a skill's `references/` and is read by code that must not hard-code it (the route table, the size table, the setup hints).

The human owns these files: edit the skill, not the Python."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "skills"


@lru_cache(maxsize=32)
def reference_text(skill: str, name: str, root: str = str(DEFAULT_ROOT)) -> str:
    p = Path(root) / skill / "references" / name
    if not p.is_file():
        raise FileNotFoundError(f"skill {skill!r} has no references/{name}")
    return p.read_text(encoding="utf-8")


def reference_json(skill: str, name: str, root: str = str(DEFAULT_ROOT)):
    return json.loads(reference_text(skill, name, root))
