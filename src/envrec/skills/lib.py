"""The one global skill library: `skills/<name>/` folders, each a valid standard skill (SKILL.md) plus the harness sidecar.

Three levels, as in the specification. The menu carries only name and description (`specs`). Choosing a skill brings its body into the model's view for that turn
(`body_for`). Scripts and references are used on demand by the runner and by the model's reads. A folder that fails validation is reported and skipped, never trusted."""

from __future__ import annotations

import ast
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from envrec.contracts import Origin, Tier, ToolSpec
from envrec.contracts.enums import TargetKind
from envrec.skills.runner import SkillRejected, check_call
from envrec.skills.skillmd import Skill, SkillFormatError, load_skill

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROOT = ROOT / "skills"
MAX_SCRIPT_BYTES = 96 * 1024
SKIP_DIRS = {"state", "generated", "__pycache__"}  # `generated` is a second root, read below


class SkillInvalid(ValueError):
    pass


def validate(skill: Skill) -> list[str]:
    """Every reason this skill may not be loaded. Empty means acceptable."""
    problems: list[str] = list(skill.problems)
    m = skill.meta
    plan = m.plan
    if plan is not None:
        seen: dict[str, set[str]] = {}
        for s_ in plan.steps:  # the same id may appear once per platform (ps is spelled differently on macOS and Linux), never twice on one
            plats = set(s_.platforms)
            prev = seen.setdefault(s_.id, set())
            if prev and ("any" in plats or "any" in prev or plats & prev):
                problems.append(f"step id {s_.id!r} appears twice for the same platform")
            prev |= plats
        for s in plan.steps:
            try:
                check_call(skill, s.tool, s.args, s.reducer)
            except SkillRejected as exc:
                problems.append(f"step {s.id}: {exc}")
        if plan.script is not None:
            sp = skill.path / plan.script
            if not sp.is_file():
                problems.append(f"the script {plan.script} does not exist")
            elif sp.stat().st_size > MAX_SCRIPT_BYTES:
                problems.append(f"the script is over {MAX_SCRIPT_BYTES // 1024} KiB")
            else:
                try:
                    tree = ast.parse(sp.read_text(encoding="utf-8"))
                    if not any(isinstance(n, ast.FunctionDef) and n.name == "parse" for n in tree.body):
                        problems.append("the script must define parse(inp)")
                except SyntaxError as exc:
                    problems.append(f"the script has a syntax error: {exc.msg} (line {exc.lineno})")
            cases = [json.loads(p.read_text()) for p in sorted((skill.path / "tests").glob("*/expect.json"))] if (skill.path / "tests").is_dir() else []
            if not any(c.get("match") is True for c in cases):
                problems.append("a skill that produces facts needs tests/: at least one recorded real capture with the expected facts")
            if not any(c.get("match") is False for c in cases):
                problems.append("a skill that produces facts needs a recorded capture that must NOT match (expect.json with \"match\": false)")
    if m.states is None and plan is None and m.produces:
        problems.append("`produces` is set but the skill has no plan")
    return problems


def _capture_leaks(folder: Path) -> list[str]:
    """Identifiers of this machine or its owner (user name, home path, hostnames, MAC addresses) in the recorded captures of a staged skill."""
    from envrec.discovery.redact import find_leaks, identity_terms
    terms = identity_terms()
    out: list[str] = []
    for f in (folder / "tests").rglob("*.txt") if (folder / "tests").is_dir() else []:
        out += [str(h)[:60] for h in find_leaks(f.read_text(errors="replace"), terms)[:1]]
    return out


@dataclass
class Library:
    root: Path = DEFAULT_ROOT
    skills: dict[str, Skill] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def load(self) -> Library:
        self.skills, self.errors = {}, []
        if not self.root.is_dir():
            return self
        gen = self.root / "generated"  # skills the model wrote live apart, and are git-ignored: their tests hold captures from real devices
        for d in sorted([*self.root.iterdir(), *(gen.iterdir() if gen.is_dir() else [])]):
            if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith(".") or not (d / "SKILL.md").exists():
                continue
            try:
                sk = load_skill(d)
            except SkillFormatError as exc:
                self.errors.append((d.name, "; ".join(exc.problems)[:400]))
                continue
            problems = validate(sk)
            if problems:
                self.errors.append((d.name, "; ".join(problems)[:400]))
                continue
            self.skills[sk.name] = sk
        return self

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name) or self.skills.get(name.replace("_", "-")) or next((s for s in self.skills.values() if s.meta.legacy_id == name), None)

    def all(self) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda s: (s.meta.ring, s.name))

    def runnable(self) -> list[Skill]:
        return [s for s in self.all() if s.meta.plan is not None]

    def protocols(self) -> list[Skill]:
        return [s for s in self.all() if s.meta.states is not None]

    # ---- the menu: name and description only -------------------------------------------------------------------------------------

    def spec(self, s: Skill) -> ToolSpec:
        kind = s.meta.signature.kind
        words = s.description.split()
        return ToolSpec(name=f"skill.{s.key}", ring=s.meta.ring, tier=Tier.observe if s.meta.tier == "observe" else Tier.read,
                        target_kinds=[TargetKind(kind)] if kind in {k.value for k in TargetKind} else list(TargetKind), one_line=" ".join(words[:12]), description=s.description[:1024],
                        origin=Origin.builtin if s.meta.provenance == "builtin" else Origin.generated, source="skills",
                        verified=s.meta.provenance == "builtin" or self.state(s.name).get("verified_runs", s.meta.verified_runs) > 0,
                        tags=["skill", s.meta.protocol] + (["generated"] if s.meta.provenance == "generated" else []), produces=list(s.meta.produces), rung=1)

    def specs(self, platform: str) -> list[ToolSpec]:
        """The menu: every skill with a plan, and every guidance skill (choosing one brings its body into the view). Protocols are not on the menu: the loop applies the one for the target's kind."""
        out = []
        for s in self.all():
            if s.meta.states is None and ("any" in s.meta.platforms or platform in s.meta.platforms):
                out.append(self.spec(s))
        return out

    def body_for(self, name: str) -> str:
        s = self.get(name)
        return f"SKILL {s.name}\n{s.body.strip()}" if s else ""

    # ---- signature matching ---------------------------------------------------------------------------------------------------------

    def match(self, target: Any, facts: dict[str, Any], platform: str) -> list[Skill]:
        import re
        out = []
        for s in self.runnable():
            sig = s.meta.signature
            if not ("any" in s.meta.platforms or platform in s.meta.platforms):
                continue
            if sig.kind and getattr(getattr(target, "kind", None), "value", None) != sig.kind:
                continue
            if sig.signature and getattr(target, "signature", None) != sig.signature:
                continue
            ok = True
            for rule in sig.facts:
                v = facts.get(rule["key"])
                if "equals" in rule and v != rule["equals"]:
                    ok = False
                if "regex" in rule and not (isinstance(v, str) and re.search(rule["regex"], v)):
                    ok = False
                if rule.get("exists") and v is None:
                    ok = False
            if ok and (sig.kind or sig.signature or sig.facts):
                out.append(s)
        return out

    # ---- per-device verification state (runtime state, kept out of the shipped files) ------------------------------------------------

    def _state_path(self, name: str) -> Path:
        return self.root / "state" / f"{name}.json"

    def state(self, name: str) -> dict:
        try:
            return json.loads(self._state_path(name).read_text())
        except (OSError, ValueError):
            return {}

    def record_verified(self, name: str, device: str) -> dict:
        st = self.state(name)
        devices = set(st.get("devices", []))
        devices.add(device)
        st = {"verified_runs": int(st.get("verified_runs", 0)) + 1, "devices": sorted(devices)}
        p = self._state_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, p)
        return st

    def verified_on(self, name: str, device: str) -> bool:
        return device in self.state(name).get("devices", [])

    # ---- adding a generated skill ------------------------------------------------------------------------------------------------------

    def add_generated(self, staged: Path, *, selftest=None) -> Path:
        """Validate a staged skill folder, run its own tests, and only then move it into the library. Nothing that fails is ever written here."""
        try:
            sk = load_skill(staged)
        except SkillFormatError as exc:
            raise SkillInvalid("; ".join(exc.problems)) from None
        if sk.meta.provenance != "generated":
            raise SkillInvalid("only generated skills are added here: a model cannot make a builtin skill")
        problems = validate(sk)
        if problems:
            raise SkillInvalid("; ".join(problems))
        if selftest is not None:
            failed = [f"{r.case}: {'; '.join(r.problems)}" for r in selftest(sk) if not r.passed]
            if failed:
                raise SkillInvalid("its own tests fail: " + " | ".join(failed)[:400])
        leaks = _capture_leaks(staged)
        if leaks:
            raise SkillInvalid(f"a recorded test capture contains an identifier of this machine or its owner ({leaks[0]}): redact it before it is kept")
        dest = self.root / "generated" / sk.name
        if dest.exists() or sk.name in self.skills:
            raise SkillInvalid(f"a skill named {sk.name!r} already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged, dest)
        self.skills[sk.name] = load_skill(dest)
        return dest
