"""Helpers for tests that need a throwaway skill folder (a SKILL.md, a sidecar, a script) built in a temp directory."""

from __future__ import annotations

import json
from pathlib import Path

from envrec.skills.skillmd import Skill, load_skill

DESC = "Use when a test needs a throwaway skill."


def make_skill(root: Path, name: str, *, steps: list[dict], script: str, produces: list[str] | None = None, platforms: list[str] | None = None, tier: str = "observe",
               provenance: str = "builtin", ring: int = 0, desc: str = DESC) -> Skill:
    d = root / name
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\nA test skill.\n")
    (d / "skill.meta.json").write_text(json.dumps({"provenance": provenance, "platforms": platforms or ["macos", "linux"], "tier": tier, "ring": ring, "produces": produces or [],
                                                   "plan": {"steps": steps, "script": "scripts/parse.py"}}))
    (d / "scripts" / "parse.py").write_text(script)
    return load_skill(d)


def step(id: str, tool: str, **args) -> dict:
    return {"id": id, "tool": tool, "args": args}


def load_data(name: str):
    import yaml
    return yaml.safe_load((Path(__file__).resolve().parent.parent / "data" / name).read_text(encoding="utf-8")) or {}


def load_agents() -> dict:
    return load_data("agents.yaml")


def load_tools() -> list[dict]:
    return load_data("tools.yaml").get("tools", [])


def load_script(skill_name: str):
    """A skill's parse script as a plain module, for testing its helper functions on the host (the sandbox is for running it on untrusted input)."""
    import importlib.util
    import sys
    lib = str(Path(__file__).resolve().parent.parent / "src" / "envrec" / "skills" / "sbxlib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    path = Path(__file__).resolve().parent.parent / "skills" / skill_name / "scripts" / "parse.py"
    spec = importlib.util.spec_from_file_location("skill_" + skill_name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
