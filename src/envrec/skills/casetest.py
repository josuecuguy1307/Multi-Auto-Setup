"""The tests a skill folder carries: `tests/<case>/captures/<name>.txt` (recorded real captures) and `tests/<case>/expect.json`.

A positive case (`"match": true`) lists the facts the script must produce from those captures; a negative case (`"match": false`) is a capture that does not belong to
this skill and must produce no fact and no entity. Cases replay recorded captures through the sandboxed script; no tool is called."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from envrec.discovery import Salts
from envrec.discovery.sanitize import clean_attrs
from envrec.discovery.types import Capture, Reason
from envrec.skills.runner import SkillRejected, collect_and_parse, to_outs
from envrec.skills.sbx import SandboxRefused, SandboxUnavailable, run_script
from envrec.skills.skillmd import Skill
from envrec.tools.ctx import ToolContext


@dataclass
class CaseResult:
    case: str
    passed: bool
    problems: list[str] = field(default_factory=list)


class _NoCollector:
    origin = "replay"

    def __init__(self, platform: str) -> None:
        self.platform = platform


def load_captures(case_dir: Path, platform: str) -> dict[str, Capture]:
    caps: dict[str, Capture] = {}
    d = case_dir / "captures"
    if not d.is_dir():
        return caps
    order = json.loads((d / "_order.json").read_text()) if (d / "_order.json").is_file() else []
    files = sorted((f for f in d.iterdir() if f.suffix == ".txt"), key=lambda f: (order.index(f.stem) if f.stem in order else len(order), f.name))
    for f in files:
        if f.suffix == ".txt":
            err = f.with_suffix(".err")
            reason = None
            detail = ""
            if err.is_file():
                reason, _, detail = err.read_text().partition("\n")
            caps[f.stem] = Capture(f.stem, f"recorded:{f.name}", f.read_bytes(), 0 if not reason else 1, "replay", platform, Reason(reason) if reason else None, detail)
    return caps


def run_case(skill: Skill, case_dir: Path, script_runner=run_script) -> CaseResult:
    expect = json.loads((case_dir / "expect.json").read_text())
    platform = expect.get("platform", "linux")
    try:
        col = collect_and_parse(skill, ToolContext(collector=_NoCollector(platform), platform=platform, salts=Salts()), {}, script_runner, preloaded=load_captures(case_dir, platform))
        outs = to_outs(skill, col)
    except (SkillRejected, SandboxRefused, SandboxUnavailable) as exc:
        return CaseResult(case_dir.name, False, [f"{type(exc).__name__}: {exc}"])
    facts = {o.key: o.value for o in outs if type(o).__name__ == "FactOut"}
    ents = list({(o.kind, o.ident): o for o in outs if type(o).__name__ == "Entity"}.values())  # the store keeps one entity per identity: the last wins
    problems: list[str] = []
    if expect.get("match") is False:
        if facts or ents:
            problems.append(f"a capture that must not match produced {len(facts)} fact(s) ({', '.join(sorted(facts))[:80]}): the script has to check that the text is from this device (its prefix) "
                            "and return nothing otherwise")
        return CaseResult(case_dir.name, not problems, problems)
    shown = False
    for k, v in expect.get("facts", {}).items():
        if k not in facts:
            problems.append(f"{k}: expected {v!r}, the skill produced no such fact" + (f" (it did produce: {', '.join(sorted(facts))})" if facts else " (it produced no fact at all: check the pattern against the capture text)"))
            shown = True
        elif json.loads(json.dumps(facts[k], default=str)) != v:
            problems.append(f"{k}: expected {v!r}, got {facts[k]!r}")
    if shown or any("got" in p for p in problems):  # help the author: what the script did produce, and what the capture looks like
        cap = next(iter(load_captures(case_dir, platform).values()), None)
        problems.append(f"the script produced {json.dumps(facts, default=str)[:160]} from a capture that begins {(cap.text[:140] if cap else '')!r}")
    if "fact_keys" in expect and sorted(facts) != sorted(expect["fact_keys"]):
        problems.append(f"fact keys: expected {sorted(expect['fact_keys'])}, got {sorted(facts)}")
    if "entities" in expect and len(ents) != expect["entities"]:
        problems.append(f"entities: expected {expect['entities']}, got {len(ents)}")
    if "entity_attrs" in expect:
        got = sorted(json.dumps(clean_attrs(e.attrs), sort_keys=True, default=str) for e in ents)  # as the privacy gate will clean them
        want = sorted(json.dumps({k: v for k, v in a.items() if k != "links"}, sort_keys=True) for a in expect["entity_attrs"])  # `links` is added by the privacy gate
        if got != want:
            problems.append("entity attributes differ from the recorded expectation")
    unknown = {o.key for o in outs if type(o).__name__ == "UnknownOut"}
    problems += [f"{k}: expected an unknown, there is none" for k in expect.get("unknown", []) if k not in unknown]
    return CaseResult(case_dir.name, not problems, problems)


def run_cases(skill: Skill, script_runner=run_script) -> list[CaseResult]:
    root = skill.path / "tests"
    return [run_case(skill, d, script_runner) for d in sorted(root.iterdir()) if d.is_dir() and (d / "expect.json").is_file()] if root.is_dir() else []
