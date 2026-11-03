"""P4: domain tags on every shipped catalog entry, written only from what the entry's OWN source states (stored captures, rules in data), and the skill_pack kind: files pinned to a commit with their SHA-256 and a
permissive license, a guard that refuses what is not pinned, a plan with one checked download per file into the pack's own folder, a guard that the environments the pack is for are on the computer, and the undo.
The seeded pack is real (its three files were fetched at the pinned commit on 2026-11-02 and are stored in fixtures/skill_pack); the run through a node and a coordinator is SIMULATED-LOCAL on loopback."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest
from acceptance_world import build
from test_executors import Origin
from test_owner_profile import CH

from envrec_coordinator import domain_tags as DT
from envrec_coordinator import tool_plan as TP
from envrec_node import executors as X
from envrec_node.setup import WriteScope
from envrec_net import envrun as E

ENVS = E.load_environments()
PERMISSIVE = set(E.load_policy()["permissive_licenses"])
SPEC = DT.load()
PACK = ENVS["legal-contract-review-skill"]
FILES = Path(__file__).parent / "fixtures" / "skill_pack"


def run(coro):
    return asyncio.run(coro)


# ---- domain tags ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_every_shipped_entry_says_where_its_domains_come_from_and_they_follow_from_the_stored_pages():
    assert set(SPEC["entries"]) == set(ENVS)  # no entry is left without sources
    for k, e in ENVS.items():
        assert DT.verify(e, SPEC) == (True, ""), k
        ds = e["domains_source"]
        assert ds["sources"] and all(len(s["capture_sha256"]) == 64 for s in ds["sources"])
        assert (e["domains"] != []) == (not ds.get("states_no_domain")) and [m["tag"] for m in ds["matched"]] == e["domains"]
    assert ENVS["opencode"]["domains"] == ["coding"] and ENVS["goose"]["domains"] == [] and "states_no_domain" in ENVS["goose"]["domains_source"] and ENVS["llamacpp-local"]["domains"] == []
    assert PACK["domains"] == ["legal"] and ENVS["meta-model-api"]["domains"] == ["coding", "image-generation", "speech-recognition"]


def test_a_tag_the_sources_do_not_support_is_found_out():
    e = copy.deepcopy(ENVS["goose"])
    e["domains"] = ["coding"]  # nobody's page says so
    assert DT.verify(e, SPEC)[0] is False
    f = copy.deepcopy(ENVS["opencode"])
    f["domains"] = []
    assert DT.verify(f, SPEC)[0] is False
    g = copy.deepcopy(ENVS["opencode"])
    g["domains_source"]["sources"][0]["capture_sha256"] = "0" * 64
    assert DT.verify(g, SPEC) == (False, "a capture is not stored")


def test_the_rules_read_only_where_they_say_and_html_descriptions_are_unescaped():
    rules = SPEC["text_rules"]
    assert [t["tag"] for t in DT.tags_from([{"description": "A legal helper", "topics": ""}], rules)] == []  # the legal rule reads topics only
    assert [t["tag"] for t in DT.tags_from([{"description": "x", "topics": "ai legal tech"}], rules)] == ["legal"]
    assert DT.page_text(b'<meta name="description" content="Meta&#x27;s coding agent for the terminal">')["description"] == "Meta's coding agent for the terminal"


# ---- the skill_pack kind -------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_seeded_pack_is_real_pinned_licensed_and_never_routable():
    assert PACK["subtype"] == "skill_pack" and PACK["license"]["spdx"] == "MIT" and len(PACK["upstream"]["commit"]) == 40 and not E.is_runnable(PACK) and PACK["pack_for"] == ["claude-code", "codex"]
    for name, f in PACK["upstream"]["files"].items():
        b = (FILES / name).read_bytes()
        assert hashlib.sha256(b).hexdigest() == f["sha256"] and len(b) == f["bytes"] and PACK["upstream"]["commit"] in f["url"]
    assert TP.install_state(PACK, PERMISSIVE) == ("installable", "") and "legal-contract-review-skill" not in E.runnable(ENVS)


def mutate(fn):
    e = copy.deepcopy(PACK)
    fn(e)
    return E.check_catalog({"x": {**e, "id": "x", "origin": "shipped"}}, PERMISSIVE)


def test_the_guard_refuses_what_is_not_pinned_licensed_or_instructions_only():
    assert mutate(lambda e: None) == []
    assert any("40-hex commit" in p for p in mutate(lambda e: e["upstream"].update(commit="main")))
    assert any("license" in p for p in mutate(lambda e: e["license"].update(spdx="GPL-3.0")))
    assert any("no adapter" in p for p in mutate(lambda e: e.update(adapter={"kind": "cli"})))
    assert any("pack_for" in p for p in mutate(lambda e: e.pop("pack_for")))
    assert any("skill.md" in p for p in mutate(lambda e: e["upstream"]["files"]["skill.md"].update(url="https://raw.githubusercontent.com/o/r/main/skill.md")))
    assert any("skill.md" in p for p in mutate(lambda e: e["upstream"]["files"]["skill.md"].pop("sha256")))
    assert any("lists its files" in p for p in mutate(lambda e: e["upstream"].update(files={})))


def test_the_plan_is_one_checked_download_per_file_into_the_packs_own_folder_and_needs_an_environment_it_is_for():
    plan = TP.build_tool_plan(PACK, {"os": "macos", "arch": "arm64", "disk": 10}, TP.load_tool_recipes(), PERMISSIVE, machine_id="m", installed={"codex"})
    assert [s.op for s in plan.steps] == ["fetch_check"] * 3 and all(s.args["dest"].startswith("<mas>/packs/legal-contract-review-skill/") and s.undo for s in plan.steps)
    assert {s.args["sha256"] for s in plan.steps} == {f["sha256"] for f in PACK["upstream"]["files"].values()} and sum(s.bytes for s in plan.steps) == sum(f["bytes"] for f in PACK["upstream"]["files"].values())
    with pytest.raises(TP.PlanRefused) as e:
        TP.build_tool_plan(PACK, {"os": "macos", "arch": "arm64", "disk": 10}, TP.load_tool_recipes(), PERMISSIVE, machine_id="m", installed={"opencode"})
    assert e.value.reason == "no_environment_for_pack"
    bad = copy.deepcopy(PACK)
    bad["upstream"]["files"]["skill.md"]["sha256"] = "zz"
    with pytest.raises(TP.PlanRefused) as e2:
        TP.build_tool_plan(bad, {"os": "macos", "arch": "arm64", "disk": 10}, TP.load_tool_recipes(), PERMISSIVE, machine_id="m", installed={"codex"})
    assert e2.value.reason == "unverified_entry"


def fake_pack(origin, bodies: dict[str, bytes]):
    commit = "c" * 40
    e = copy.deepcopy(PACK)
    e.update({"id": "fakepack", "title": "Fake pack", "pack_for": ["llamacpp-local"]})
    e["upstream"] = {**e["upstream"], "commit": commit, "files": {n: {"url": origin.url(f"/{commit}/{n}"), "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)} for n, b in bodies.items()}}
    return e, commit


def pack_recipe():
    r = copy.deepcopy(TP.load_tool_recipes()["skill-pack-files"])
    r["download"] = {"schemes": ["http"], "hosts": {"127.0.0.1": ["127.0.0.1"]}, "max_redirects": 3, "disk_margin": 1.0}
    return r


REAL_PACK_RECIPE = TP.load_tool_recipes()["skill-pack-files"]


def test_the_node_puts_each_file_in_the_pack_folder_checks_it_and_the_undo_leaves_nothing(tmp_path):
    bodies = {"skill.md": b"# a skill\n", "LICENSE": b"MIT\n"}
    commit = "c" * 40
    o = Origin({f"/{commit}/{n}": b for n, b in bodies.items()})
    try:
        entry, _ = fake_pack(o, bodies)
        rec = copy.deepcopy(REAL_PACK_RECIPE)
        rec["download"] = {"schemes": ["http"], "hosts": {"127.0.0.1": ["127.0.0.1"]}, "max_redirects": 3, "disk_margin": 1.0}
        plan = TP.build_tool_plan(entry, {"os": "linux", "arch": "amd64", "disk": 10}, {"skill-pack-files": rec}, PERMISSIVE, machine_id="m", installed={"llamacpp-local"})
        home = tmp_path / "mas"
        home.mkdir()
        for s in plan.steps:
            X.fetch_check(dict(s.args), WriteScope(home, list(s.writes)), {"recipe": rec, "mas_home": home})
        assert (home / "packs" / "fakepack" / "skill.md").read_bytes() == bodies["skill.md"]
        bad = copy.deepcopy(plan.steps[0].args)
        bad["sha256"] = "1" * 64
        bad["dest"] = "<mas>/packs/fakepack/other.md"
        with pytest.raises(X.StepFailed):
            X.fetch_check(bad, WriteScope(home, ["<mas>/packs/fakepack/other.md"]), {"recipe": rec, "mas_home": home})
        assert not (home / "packs" / "fakepack" / "other.md").exists()
        for s in reversed(plan.steps):
            X.undo_fetch_check(dict(s.args), WriteScope(home, list(s.writes)), {"recipe": rec, "mas_home": home})
        assert not (home / "packs").exists()
    finally:
        o.close()


def test_through_the_services_one_question_then_the_files_are_placed_and_recorded_and_the_undo_removes_them(tmp_path, monkeypatch):
    async def go():
        bodies = {"skill.md": b"# a skill\n"}
        commit = "c" * 40
        o = Origin({f"/{commit}/skill.md": bodies["skill.md"]})
        entry, _ = fake_pack(o, bodies)
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            rec = copy.deepcopy(REAL_PACK_RECIPE)
            rec["download"] = {"schemes": ["http"], "hosts": {"127.0.0.1": ["127.0.0.1"]}, "max_redirects": 3, "disk_margin": 1.0}
            c.registry.save_analysis(nid, {"facts": {"machine.os.family": {"value": "linux", "status": "tested"}, "machine.os.arch": {"value": "x86_64", "status": "tested"}, "machine.disk_free_gib": {"value": 200, "status": "tested"},
                                                     "machine.ai_clis": {"value": [], "status": "tested"}, "machine.api_key_variable_names": {"value": [], "status": "tested"}}}, 1.0)
            monkeypatch.setattr(TP, "load_tool_recipes", lambda path=None: {"skill-pack-files": rec})
            c.services.catalog["fakepack"] = entry
            ctx = w.agent.runner.ctx
            ctx["recipes"] = {**ctx["recipes"], "skill-pack-files": rec}
            r = c.services.install_tool(CH, environment="fakepack")
            assert r["ok"] and "Install Fake pack" in r["text"]
            c.harness.decide(CH, r["intent_id"], "approve")
            job = c.services.jobs[r["job_id"]]
            for _ in range(300):
                if job.state in ("done", "failed"):
                    break
                await asyncio.sleep(0.1)
            assert job.state == "done", job.text
            assert (Path(ctx["mas_home"]) / "packs" / "fakepack" / "skill.md").read_bytes() == bodies["skill.md"] and "fakepack" in c.runs.stored_tools(nid)
            r2 = c.services.install_tool(CH, environment="fakepack", action="undo")
            c.harness.decide(CH, r2["intent_id"], "approve")
            job2 = c.services.jobs[r2["job_id"]]
            for _ in range(300):
                if job2.state in ("done", "failed"):
                    break
                await asyncio.sleep(0.1)
            assert job2.state == "done" and not (Path(ctx["mas_home"]) / "packs").exists() and c.runs.stored_tools(nid) == {}
        finally:
            await teardown()
            o.close()
    run(go())
