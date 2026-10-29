"""Read and check one skill folder against the Agent Skills specification (https://agentskills.io/specification) plus this harness's own rules.

Specification rules (exact): `SKILL.md` starts with YAML frontmatter; `name` is 1-64 characters of lowercase letters, digits and single hyphens, never leading or
trailing, and equals the folder name; `description` is 1-1024 characters; `license`, `compatibility` (max 500), `metadata` (string to string) and the experimental
`allowed-tools` are the only other keys.

Harness rules on top: the description says only WHEN to use the skill, starts with "Use when" and does not summarize a workflow (a model that reads a workflow summary
follows it instead of the body); the body is under 500 lines."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from envrec.skills.meta import SkillMeta

ALLOWED_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_BODY_LINES = 500
_WORKFLOW = re.compile(r"(\bthen\b|\bafter that\b|\bafterwards\b|\bnext,|\bfinally\b|\bstep \d|\bfirst,|->|→|\b\d[.)]\s)", re.I)


class SkillFormatError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Skill:
    path: Path
    name: str
    description: str
    body: str
    frontmatter: dict
    meta: SkillMeta
    problems: list[str] = field(default_factory=list)  # lint findings that do not stop it loading (reported by `skills verify`)

    @property
    def key(self) -> str:
        """The name as the menu and the fact store spell it (the store's namespace has no hyphen in the first segment)."""
        return self.name.replace("-", "_")

    @property
    def id(self) -> str:
        return self.meta.legacy_id or self.key

    # the few meta fields discovery and the CLI read all the time
    @property
    def ring(self) -> int:
        return self.meta.ring

    @property
    def tier(self) -> str:
        return self.meta.tier

    @property
    def platforms(self) -> list[str]:
        return self.meta.platforms

    @property
    def mode(self) -> str:
        """survey: runs by itself before the first model turn (observe tier). menu: the model chooses it and a human approves what it sends (read tier)."""
        return "survey" if self.meta.tier == "observe" else "menu"


def lint_description(desc: str) -> list[str]:
    out = []
    if not desc.startswith("Use when"):
        out.append('the description must start with "Use when": it says when to use the skill, not what the skill is')
    m = _WORKFLOW.search(desc)
    if m:
        out.append(f"the description summarizes a workflow (found {m.group(0)!r}): say only when to use the skill; the steps belong in the body")
    return out


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"\A---\n(.*?)\n---[ \t]*\n?(.*)\Z", text, re.S)
    if not m:
        raise SkillFormatError(["SKILL.md must start with a YAML frontmatter block between --- lines"])
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise SkillFormatError([f"the frontmatter is not valid YAML: {str(exc)[:120]}"]) from None
    if not isinstance(fm, dict):
        raise SkillFormatError(["the frontmatter must be a mapping"])
    return fm, m.group(2).lstrip("\n")


def check_frontmatter(fm: dict, folder: str) -> list[str]:
    p: list[str] = []
    extra = sorted(set(fm) - ALLOWED_KEYS)
    if extra:
        p.append(f"unexpected frontmatter key(s) {extra}; the standard allows only {sorted(ALLOWED_KEYS)} (harness data goes in skill.meta.json)")
    name = fm.get("name")
    if not isinstance(name, str) or not name:
        p.append("missing `name`")
    else:
        if len(name) > 64:
            p.append(f"`name` is {len(name)} characters; the maximum is 64")
        if not NAME_RE.match(name):
            p.append(f"`name` {name!r} must be lowercase letters, digits and single hyphens, not starting or ending with a hyphen")
        if name != folder:
            p.append(f"`name` {name!r} must equal the folder name {folder!r}")
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        p.append("missing `description`")
    elif len(desc) > 1024:
        p.append(f"`description` is {len(desc)} characters; the maximum is 1024")
    comp = fm.get("compatibility")
    if comp is not None and (not isinstance(comp, str) or not 1 <= len(comp) <= 500):
        p.append("`compatibility` must be a string of 1 to 500 characters")
    md = fm.get("metadata")
    if md is not None and (not isinstance(md, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in md.items())):
        p.append("`metadata` must map strings to strings")
    if "allowed-tools" in fm and not isinstance(fm["allowed-tools"], str):
        p.append("`allowed-tools` must be a space-separated string")
    return p


def load_skill(folder: Path) -> Skill:
    """Parse one skill folder. Raises SkillFormatError with every problem found, never a partial skill."""
    md = folder / "SKILL.md"
    if not md.is_file():
        raise SkillFormatError(["SKILL.md not found"])
    fm, body = split_frontmatter(md.read_text(encoding="utf-8"))
    problems = check_frontmatter(fm, folder.name)
    lines = len(body.splitlines())
    if lines > MAX_BODY_LINES:
        problems.append(f"the body is {lines} lines; the limit is {MAX_BODY_LINES}: move variants into references/")
    desc = fm.get("description") if isinstance(fm.get("description"), str) else ""
    problems += lint_description(desc)
    meta_path = folder / "skill.meta.json"
    try:
        meta = SkillMeta.model_validate(json.loads(meta_path.read_text(encoding="utf-8"))) if meta_path.is_file() else SkillMeta()
    except ValueError as exc:
        problems.append(f"skill.meta.json is invalid: {str(exc)[:200]}")
        meta = SkillMeta()
    if problems:
        raise SkillFormatError(problems)
    return Skill(folder, fm["name"], desc.strip(), body, fm, meta)
