"""What the setup downloads is pinned, and nothing a setup stores or logs looks like a credential."""
from __future__ import annotations

import copy
import json

import pytest

from envrec.privacy.secrets import scan
from envrec_coordinator import options as O
from envrec_coordinator.setup_plan import check_pins, load_recipes

RULES, CAT, RECIPES = O.load_rules(), O.load_catalog(), load_recipes()


def test_every_runtime_build_and_model_file_the_recipe_would_download_is_pinned_and_on_an_allowed_host():
    assert check_pins(CAT, RECIPES, RULES) == []
    assert len(CAT["runtime_assets"]) >= 12 and len(CAT["model_files"]) >= 150  # it looked at the whole catalog, not at nothing


@pytest.mark.parametrize("what,change,expect", [
    ("a plain http address", lambda c: c["model_files"][0].update(url=c["model_files"][0]["url"].replace("https://", "http://")), "scheme 'http'"),
    ("a host the recipe does not name", lambda c: c["model_files"][0].update(url="https://evil.example/x.gguf"), "evil.example"),
    ("a missing checksum", lambda c: c["model_files"][0].update(sha256="abc"), "no pinned SHA-256"),
    ("a moving revision", lambda c: c["model_files"][0].update(url=c["model_files"][0]["url"].replace(c["model_files"][0]["revision"], "main")), "not pinned to its revision"),
    ("a runtime without a commit", lambda c: c["runtime_assets"][0].update(commit=None), "no pinned commit"),
    ("a runtime with no size", lambda c: c["runtime_assets"][0].update(bytes=0), "no size"),
])
def test_an_unpinned_or_off_list_download_is_found(what, change, expect):
    cat = copy.deepcopy(CAT)
    change(cat)
    assert any(expect in p for p in check_pins(cat, RECIPES, RULES)), what


def test_a_recipe_missing_a_command_an_undo_or_a_server_program_for_an_os_family_is_found():
    rec = copy.deepcopy(RECIPES)
    r = rec["local-server"]
    del r["commands"]["fetch_check"]["windows"]
    del r["undo"]["unpack_archive"]["linux"]
    del r["server"]["binary"]["macos"]
    del r["backend_args"]["metal"]
    problems = " | ".join(check_pins(CAT, rec, RULES))
    assert "no command for fetch_check on windows" in problems and "no undo for unpack_archive on linux" in problems and "no server program name for macos" in problems and "metal backend" in problems


@pytest.mark.parametrize("secret", ["sk-abcdefghijklmnop12345", "ghp_" + "a" * 30, "hf_" + "B" * 30, "AKIA" + "A" * 16, "-----BEGIN RSA PRIVATE KEY-----", "Authorization: Bearer " + "x" * 30,
                                    '{"api_key": "hunter2hunter2"}', "password=correcthorse", '"token": "abcdef123456"'])
def test_the_secret_scan_finds_a_credential_shaped_string_and_masks_it(secret):
    hits = scan({"note": secret})
    assert hits and all("sample" in h and secret not in h["sample"] for h in hits)


@pytest.mark.parametrize("fine", ["OPENAI_API_KEY", {"name": "ANTHROPIC_API_KEY", "provider": "anthropic"}, {"tokens_per_s": 12.5, "tokens": 2}, "sha256:" + "a" * 64, "a model that answers in 40 ms", {"owner_only": True, "key": {"path": "k.pem"}}])
def test_the_secret_scan_does_not_flag_names_of_variables_hashes_or_counts(fine):
    assert scan(fine) == []


def test_nothing_a_setup_run_stores_or_logs_looks_like_a_credential_and_the_scan_would_have_caught_one(tmp_path):
    import asyncio

    from envrec.model import ScriptedModel
    from mas_world import coordinator
    from test_mas_agent import AVAILABLE, act, analysed, down, node_up
    from test_setup_run import LOOSE, Origin, make_world, owner_approves, record_pick

    async def go():
        origins = []
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            facts = {k: row["value"] for k, row in c.runs.stored_analysis(nid)["facts"].items()}
            catalog, recipes = make_world(lambda f: origins.append(Origin(f)) or origins[-1], facts)
            record_pick(c, nid, catalog)
            agent.runner.ctx.update({"recipes": recipes})
            script = [act("load_context"), act("recheck_fit"), act("build_plan")] + [act("run_step", args={"step": str(n)}) for n in range(1, 7)] + [act("undo_step", args={"step": str(n)}) for n in range(6, 0, -1)]
            r = c.runs.start_install(nid, "web", model=ScriptedModel(script), status=AVAILABLE, recipes=recipes, catalog=catalog, rules=LOOSE)
            approver = owner_approves(c, r)
            await asyncio.wait_for(r.task, 120)
            await approver
            stored = {"facts": [json.loads(f.model_dump_json()) for f in r.store.current_facts(r.target.id)], "events": [json.loads(e.model_dump_json()) for e in r.store.events()],
                      "coordinator_log": [json.loads(e.model_dump_json()) for e in c.log.events()], "node_log": [json.loads(e.model_dump_json()) for e in agent.log.events()],
                      "tasks": [t.view() for t in c.tasks.list()], "view": c.runs.view(r.id), "analysis": c.runs.stored_analysis(nid), "pick": c.runs.stored_pick(nid)}
            assert scan(stored) == [], scan(stored)
            assert scan({**stored, "planted": "Authorization: Bearer " + "z" * 40}) != []  # the same scan over the same data does catch one that is planted
            await c.runs.shutdown()
            await down(agent, task)
        for o in origins:
            o.close()
    asyncio.run(go())
