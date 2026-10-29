"""How a model makes a skill: it fills in a DRAFT (one JSON object), and code lays the draft out as a skill folder.

The model never writes frontmatter, a sidecar or a directory tree by hand: those are mechanical, so code does them and cannot get them wrong. What the model contributes is
judgment (when the skill applies, what it does, how to read the capture) and the script and tests that prove it. The staged folder then goes through exactly the same
checks as any other skill, plus its own tests in the sandbox; nothing reaches the library unless all of them pass."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, model_validator

from envrec.contracts.base import Strict
from envrec.skills.casetest import run_cases
from envrec.skills.lib import Library, SkillInvalid, validate
from envrec.skills.meta import PlanStep
from envrec.skills.skillmd import Skill, SkillFormatError, load_skill


class DraftTest(Strict):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{0,39}$")
    platform: str = "linux"
    captures: dict[str, str]  # capture name -> the recorded text
    match: bool = True  # False: a capture that does NOT belong to this skill; the skill must produce nothing from it
    facts: dict[str, Any] = Field(default_factory=dict)  # expected fact key -> value (positive cases)
    entities: int | None = None


class Draft(Strict):
    name: str
    description: str
    body: str
    script: str
    steps: list[PlanStep]
    produces: list[str]
    tests: list[DraftTest]
    protocol: str = "generic"
    tier: str = "observe"
    ring: int = Field(default=1, ge=0, le=3)
    platforms: list[str] = Field(default_factory=lambda: ["any"])
    signature: dict[str, Any] = Field(default_factory=dict)
    identify: bool = False  # run at the first rung of a later run to produce the facts a signature matches (set for recipe skills)
    recipe: dict[str, Any] | None = None  # a workspace recipe (shell, root, port, steps, health, absent, bind_check, rss): checked statically and by a dry run before anything is kept

    @model_validator(mode="after")
    def _tests(self) -> Draft:
        if not any(t.match for t in self.tests):
            raise ValueError("the draft has no test with a recorded real capture and the facts it must produce (match: true)")
        if not any(not t.match for t in self.tests):
            raise ValueError("the draft has no test with a capture that must NOT match (match: false)")
        return self


class DraftError(ValueError):
    """The draft is not acceptable. The message names each mistake so the author can fix exactly that."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def parse_draft(data: Any) -> Draft:
    try:
        return Draft.model_validate(data)
    except ValidationError as exc:
        hint = "each step is {id, tool, args}; each test is {name, captures: {capture name: text or \"@capture:name\"}, match: true|false, facts: {key: value}}"
        raise DraftError([*(f"{'.'.join(str(x) for x in e['loc']) or 'draft'}: {e['msg']}" for e in exc.errors()[:4]), hint]) from None


def stage_draft(data: Any, root: Path) -> Path:
    """Lay the draft out as a skill folder under `root` (a scratch directory). The result is a generated skill by construction."""
    d = parse_draft(data)
    folder = root / d.name
    if folder.exists():
        shutil.rmtree(folder)
    (folder / "scripts").mkdir(parents=True)
    desc = " ".join(d.description.split())
    (folder / "SKILL.md").write_text(f"---\nname: {d.name}\ndescription: {json.dumps(desc)}\n---\n\n{d.body.strip()}\n", encoding="utf-8")
    (folder / "scripts" / "parse.py").write_text(d.script, encoding="utf-8")
    meta = {"provenance": "generated", "protocol": d.protocol, "tier": d.tier, "ring": d.ring, "platforms": d.platforms, "signature": d.signature,
            "plan": {"steps": [s.model_dump(exclude_defaults=True) | {"id": s.id, "tool": s.tool} for s in d.steps], "script": "scripts/parse.py"}, "produces": d.produces,
            "script_sha256": hashlib.sha256(d.script.encode()).hexdigest(), **({"recipe": d.recipe, "identify": True} if d.recipe else ({"identify": True} if d.identify else {}))}
    (folder / "skill.meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    for t in d.tests:
        c = folder / "tests" / t.name
        (c / "captures").mkdir(parents=True)
        for name, text in t.captures.items():
            (c / "captures" / f"{name}.txt").write_text(text, encoding="utf-8")
        expect: dict[str, Any] = {"platform": t.platform, "match": t.match}
        if t.match:
            expect["facts"] = t.facts
            if t.entities is not None:
                expect["entities"] = t.entities
        (c / "expect.json").write_text(json.dumps(expect, indent=1), encoding="utf-8")
    return folder


def check_staged(folder: Path, script_runner=None) -> tuple[Skill | None, list[str]]:
    """Every reason this staged skill is not acceptable (empty list: acceptable), including its own tests run in the sandbox."""
    try:
        sk = load_skill(folder)
    except SkillFormatError as exc:
        return None, list(exc.problems)
    problems = validate(sk)
    if problems:
        return sk, problems
    if sk.meta.recipe is not None:
        from envrec.recipes.dryrun import dry_run
        from envrec.recipes.model import problems as recipe_problems
        bad = recipe_problems(sk.meta.recipe) or dry_run(sk.meta.recipe)
        if bad:
            return sk, [f"recipe: {b}" for b in bad]
    kw = {"script_runner": script_runner} if script_runner is not None else {}
    return sk, [f"test {r.case}: {'; '.join(r.problems)}" for r in run_cases(sk, **kw) if not r.passed]


def add_if_valid(lib: Library, data: Any, scratch: Path, script_runner=None) -> Path:
    """Stage, check, and only then add to the library. Raises DraftError / SkillInvalid naming the mistake; nothing is written to the library otherwise."""
    folder = stage_draft(data, scratch)
    _, problems = check_staged(folder, script_runner)
    if problems:
        raise SkillInvalid("; ".join(problems))
    return lib.add_generated(folder)


def authoring_context(lib: Library, example: str) -> str:
    """What a model is given to write a skill: the authoring skill and ONE worked example (its instructions, its script and one of its tests). Nothing else."""
    a, ex = lib.get("authoring"), lib.get(example)
    if a is None or ex is None or not ex.meta.example:
        raise SkillInvalid(f"{example!r} is not a worked example in the library")
    case = next(iter(sorted((ex.path / "tests").glob("*/expect.json"))), None)
    parts = [f"SKILL authoring\n{a.body.strip()}", f"WORKED EXAMPLE {ex.name}\n{ex.body.strip()}", f"scripts/parse.py of {ex.name}:\n{(ex.path / 'scripts/parse.py').read_text()}"]
    if case is not None:
        cap_dir = case.parent / "captures"
        parts.append(f"a test of {ex.name} ({case.parent.name}): expect.json\n{case.read_text()}\n" + "\n".join(f"captures/{f.name}:\n{f.read_text()[:600]}" for f in sorted(cap_dir.glob('*.txt'))[:3]))
    return "\n\n".join(parts)
