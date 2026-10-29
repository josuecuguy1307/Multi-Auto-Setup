#!/usr/bin/env python3
"""Record a skill's tests from the committed fixtures and check them against the golden outputs frozen before the migration.

  python scripts/record_skill_tests.py [skill-name ...]

For every fixture of a skill (fixtures/discovery/<id>/<platform>/, recorded real captures) this runs the whole skill (tools, sandboxed script, privacy gate), compares the
result with the golden output of the adapter it replaced, and, only when they are identical, writes `skills/<name>/tests/<fixture>/` (every capture the run used, lookup
answers included, plus `expect.json`). It also writes two cases that must NOT match: every read failed, and every read printed something unrelated."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import golden_tables  # noqa: E402
from envrec.contracts import Target, TargetKind  # noqa: E402
from envrec.discovery import ReplayCollector, Salts  # noqa: E402
from envrec.skills.casetest import run_cases  # noqa: E402
from envrec.skills.lib import Library  # noqa: E402
from envrec.skills.runner import run_skill  # noqa: E402
from envrec.skills.skillmd import load_skill  # noqa: E402
from envrec.store import Store  # noqa: E402
from envrec.tools.ctx import ToolContext  # noqa: E402

GOLD = ROOT / "tests" / "golden" / "discovery"
FIX = ROOT / "fixtures" / "discovery"


def fixed_salts() -> Salts:
    s = Salts()
    s.run, s._stable = "GOLDEN-RUN-SALT", "GOLDEN-STABLE-SALT"
    return s


def run_fixture(skill, fixture_dir: Path, platform: str):
    with tempfile.TemporaryDirectory() as t, Store(Path(t) / "s") as st:
        host = st.put_target(Target(kind=TargetKind.host, ring=skill.meta.ring, handle="golden-host"))
        ctx = ToolContext(collector=ReplayCollector(fixture_dir, platform), platform=platform, store=st, target=host, salts=fixed_salts(), approve=lambda text: True)
        run = run_skill(skill, ctx, params={})
        facts = sorted(({"key": f.key, "status": f.status.value, "value": f.value} for f in st.current_facts(host.id) if not f.key.startswith("ring.adapter.")), key=lambda x: x["key"])
    return run, facts


def differs(run, facts, g) -> list[str]:
    out = []
    if run.status != g["status"]:
        out.append(f"status {run.status!r} != {g['status']!r} ({run.reason[:200]})")
    counts = {"entities": run.entities, "resolved": run.resolved, "unknowns": run.unknowns, "facts": run.facts}
    if counts != g["counts"]:
        out.append(f"counts {counts} != {g['counts']}")
    if run.unknown_reasons != g["unknown_reasons"]:
        out.append(f"unknown reasons {run.unknown_reasons} != {g['unknown_reasons']}")
    a, b = json.loads(json.dumps(facts, default=str)), json.loads(json.dumps(g["facts"], default=str))
    if a != b:
        ka, kb = {f["key"]: f for f in a}, {f["key"]: f for f in b}
        for k in sorted(set(ka) | set(kb)):
            if ka.get(k) != kb.get(k):
                out.append(f"fact {k}: got {json.dumps(ka.get(k), default=str)[:240]} want {json.dumps(kb.get(k), default=str)[:240]}")
                if len(out) > 6:
                    break
    return out


def write_case(skill, case: str, platform: str, caps: dict, expect: dict) -> None:
    d = skill.path / "tests" / case
    if d.exists():
        shutil.rmtree(d)
    (d / "captures").mkdir(parents=True)
    (d / "captures" / "_order.json").write_text(json.dumps(list(caps)))  # scripts that read "the first capture" must see the same order as when it was recorded
    for name, c in caps.items():
        (d / "captures" / f"{name}.txt").write_bytes(c.data)
        if c.error is not None:
            (d / "captures" / f"{name}.err").write_text(f"{c.error.value}\n{c.detail}")
    (d / "expect.json").write_text(json.dumps({"platform": platform, **expect}, indent=1, sort_keys=True, default=str))


def main(argv: list[str]) -> int:
    golden_tables.install()
    lib = Library()
    bad = 0
    names = argv or sorted(p.name for p in (ROOT / "skills").iterdir() if (p / "SKILL.md").exists() and not p.name.startswith("protocol-"))
    for name in names:
        skill = load_skill(ROOT / "skills" / name)
        legacy = skill.meta.legacy_id
        if not legacy:
            continue
        golds = sorted(GOLD.glob(f"{legacy}__*.json"))
        wrote = 0
        first_caps = None
        for gf in golds:
            g = json.loads(gf.read_text())
            fx = FIX / legacy / g["fixture"]
            run, facts = run_fixture(skill, fx, g["platform"])
            diff = differs(run, facts, g)
            if diff:
                bad += 1
                print(f"MISMATCH {name} [{g['fixture']}]\n    " + "\n    ".join(diff))
                continue
            col = run.collected
            plain = {f["key"]: f["value"] for f in facts if ".entity." not in f["key"] and f["status"] != "unknown"}
            ents = [f["value"]["attrs"] for f in facts if ".entity." in f["key"]]
            unknown = [f["key"] for f in facts if f["status"] == "unknown"]
            write_case(skill, g["fixture"], g["platform"], col.caps, {"match": True, "facts": plain, "entities": len(ents), "entity_attrs": ents, "unknown": unknown})
            wrote += 1
            if first_caps is None:
                first_caps = (g["platform"], col)
        if first_caps:
            plat, col = first_caps
            from envrec.discovery.types import Capture, Reason
            failed = {n: Capture(n, "recorded", b"", None, "replay", plat, Reason.unavailable, "the program is not installed or the file does not exist") for n in col.caps}
            write_case(skill, "all-reads-failed", plat, failed, {"match": False})
            junk = {n: Capture(n, "recorded", b"Segmentation fault: 11\n", 0, "replay", plat) for n in col.caps}
            write_case(skill, "unrelated-output", plat, junk, {"match": False})
            from envrec.skills.casetest import run_case
            if not run_case(load_skill(ROOT / "skills" / name), skill.path / "tests" / "unrelated-output").passed:
                shutil.rmtree(skill.path / "tests" / "unrelated-output")  # a tolerant parser (it reads what it is given) has no unrelated-output case; the failed-reads case remains
        results = run_cases(load_skill(ROOT / "skills" / name)) if wrote else []
        failing = [r for r in results if not r.passed]
        for r in failing:
            bad += 1
            print(f"CASE FAILS {name}/{r.case}: {r.problems[:2]}")
        print(f"{'ok ' if wrote and not failing else 'BAD'} {name}: {wrote} fixture case(s) recorded, {len(results)} case(s) pass" if wrote else f"no fixtures matched for {name}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
