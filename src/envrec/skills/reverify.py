"""Per-device skill re-verification.

A skill verified on one device says nothing about another. On any other device it is `unverified_here` until
its stored tests pass THERE: the skill's code is run on that device's own recorded data and must produce
valid output with the same fact keys the skill was verified for. Only then may it be offered at rung-1
speed, and only then are its facts on that device marked `verified`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from envrec.contracts import Actor, Event, RawRef, Skill
from envrec.ladder.generate import ReplayRunner, read_skill_code
from envrec.store import Store

VERIFIED_HERE, UNVERIFIED_HERE = "verified_here", "unverified_here"


@dataclass
class ReverifyResult:
    passed: bool
    reason: str
    facts: list[dict]  # facts from the first tested chunk, for the caller to record


def skill_status_here(store: Store, skill: Skill, target_id: str) -> str:
    for e in store.events(type="skill_verified_here"):
        if e.payload.get("skill") == skill.id and e.payload.get("target") == target_id:
            return VERIFIED_HERE
    return UNVERIFIED_HERE


def mark_verified_here(store: Store, skill: Skill, target_id: str, how: str) -> None:
    store.append_event(Event(actor=Actor.verifier, type="skill_verified_here",
                             payload={"skill": skill.id, "target": target_id, "how": how}))


def _load_tests(store: Store, skill: Skill) -> dict:
    return json.loads(store.read_raw(RawRef(path=skill.tests_ref, sha256=skill.tests_ref.rsplit("/", 1)[-1])))


def reverify_skill(store: Store, skill: Skill, target_id: str, device_chunks: list[str],
                   runner: ReplayRunner) -> ReverifyResult:
    """Run the skill's stored tests on THIS device's data. Records `skill_verified_here` only on success."""
    tests = _load_tests(store, skill)
    expected = tuple(sorted(tests["expected_keys"]))
    if not device_chunks:
        return ReverifyResult(False, "this device has no recorded data to test on", [])
    code = read_skill_code(store, skill)
    first: list[dict] = []
    for i, chunk in enumerate(device_chunks[: max(len(tests.get("chunks", [])), 3)]):
        r = runner.run(code, chunk)
        if not r.ok:
            return ReverifyResult(False, f"the skill failed its test on this device (chunk {i}): {r.error}", [])
        keys = tuple(sorted(f["key"] for f in r.facts))
        if keys != expected:
            return ReverifyResult(False, f"the skill produced {list(keys)} here, expected {list(expected)}", [])
        if i == 0:
            first = r.facts
    mark_verified_here(store, skill, target_id, "stored tests re-run on this device")
    return ReverifyResult(True, "stored tests passed on this device", first)
