"""protocol-draft: the draft of the owner's whole network. The hard limits are code (GUARD); inside them a MODEL proposes; the OWNER's words bind and are recorded as OWNER events.
The machines are SIMULATED-LOCAL nodes built from stored typed results of two REAL analyses (a Windows 11 laptop, the coordinator Mac); Linux is UNTESTED (see the synthetic check at the end);
the model is scripted (the real local model is exercised by the verification run)."""
from __future__ import annotations

import asyncio
import itertools
import json
from collections import Counter
from pathlib import Path

import pytest

from envrec.model import ScriptedModel
from envrec_coordinator import draft as D
from envrec_coordinator import options as O
from mas_world import coordinator

ROOT = Path(__file__).resolve().parent.parent
AVAILABLE = {"available": True, "profile": "scripted", "reason": ""}
WIN, MAC = "n-win", "n-mac"


def run(coro):
    return asyncio.run(coro)


def act(tool=None, action="call_tool", **kw):
    d = {"action": action, "why": kw.pop("why", "next step")}
    if tool:
        d["tool"] = tool
    d.update(kw)
    return json.dumps(d)


def stored(path: str, node: str) -> dict:
    d = json.loads((ROOT / "fixtures" / "probes" / path).read_text())
    facts = {k: {"value": v["value"], "status": v["status"], "probe": v["probe"]} for k, v in d["facts"].items()}
    facts["analysis.finished"] = {"value": True, "status": "tested", "probe": "fixture"}  # the fixture builder's record: no agent run made these two stored analyses
    return {"run": f"fixture-{node}", "node": node, "stored_at": 1.0, "facts": facts}


def net(*rows):
    return lambda: [{"node_id": i, "name": n, "stored": s} for i, n, s in rows]


def two():
    return net((WIN, "win-laptop", stored("windows-real/typed_facts.redacted.json", WIN)), (MAC, "mac-coordinator", stored("darwin-real/typed_facts.json", MAC)))


def facts_of_fixture(path: str) -> dict:
    return {k: v["value"] for k, v in json.loads((ROOT / "fixtures" / "probes" / path).read_text())["facts"].items()}


WINF, MACF = facts_of_fixture("windows-real/typed_facts.redacted.json"), facts_of_fixture("darwin-real/typed_facts.json")
NOCON = D.Constraints()
# the local model the Mac is offered. Ollama is on the Mac but has no setup recipe yet (options: no_recipe_yet), so the draft must not propose it: the pick is a catalog model with a recipe
MAC_MODEL = next(c for c in D.machine_choices(MAC, {"id": MAC, "name": MAC}, MACF, NOCON)["choices"] if "/own_harness/" in c).rsplit("/", 1)[1]
REFS_WIN = ["network.machine.n_win", "default:unrestricted_resources"]
REFS_MAC = ["network.machine.n_mac", "default:unrestricted_resources"]
REFS_TEAM = ["network.machine.n_mac", "default:no_task_info", "default:ask_before_acting", "default:owner_present"]


def win_provider(**over):
    a = {"choice": "n-win/provider/subscription:claude", "resources": ["plan"], "because": "It already has Claude signed in.", "rests_on": REFS_WIN}
    a.update(over)
    return act("draft_machine", args=a)


def mac_own(model=MAC_MODEL, **over):
    a = {"choice": f"n-mac/own_harness/local_hardware/{model}", "resources": ["local_models"], "because": "A local model fits this machine.", "rests_on": REFS_MAC}
    a.update(over)
    return act("draft_machine", args=a)


def win_local(**over):
    first = next(c for c in D.machine_choices(WIN, {"id": WIN, "name": WIN}, WINF, NOCON)["choices"] if "/own_harness/" in c)
    a = {"choice": first, "resources": ["local_models"], "because": "Local models only, as asked.", "rests_on": REFS_WIN}
    a.update(over)
    return act("draft_machine", args=a)


def team(**over):
    a = {"tasks": "coding and document review", "team": "director_workers/n-mac", "freedom": "propose_only", "rhythm": "owner_present", "because": "The Mac has the most memory.", "rests_on": REFS_TEAM}
    a.update(over)
    return act("draft_network", args=a)


def finish(text="I propose the Mac directs; everything else is an assumption."):
    return act("finish_draft", args={"summary": text})


GOOD = [act("load_network"), win_provider(), mac_own(), team(), finish()]


async def drive(c, replies, *, network=None, correction=None, via="web", answers=None):
    r = c.runs.start_draft(via, correction=correction, model=ScriptedModel(list(replies)), status=AVAILABLE, network=network or two())
    waiter = asyncio.create_task(answer_all(c, r, answers or {}))
    try:
        await asyncio.wait_for(r.task, 60)
    finally:
        waiter.cancel()
    return r


async def answer_all(c, r, answers: dict):
    while True:
        p = r.adapter.pending if r.adapter else None
        if p is not None and p.qid in answers:
            c.answer_run(r.id, p.qid, answers[p.qid])
        await asyncio.sleep(0.02)


def errors(r):
    return [e.payload.get("error", "") for e in r.store.events() if e.type == "tool_result" and not e.payload.get("ok", True)]


def tags(r):
    return Counter(e.effective_tag for e in r.store.events())


# ---------------------------------------------------------------------------------------------------------------- the protocol, end to end

def test_a_draft_of_two_machines_of_different_os_closes_by_the_guard_after_the_models_own_choices(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, GOOD)
            assert r.status == "done" and r.summary["stop_reason"] == "protocol_done" and r.summary["tools_run"] == ["load_network", "draft_machine", "draft_machine", "draft_network", "finish_draft"]
            closes = [e for e in r.store.events() if e.type == "protocol_state" and e.payload.get("to") == "done"]
            assert len(closes) == 1 and closes[0].effective_tag == "GUARD" and closes[0].payload.get("closed_by") == "finish_draft"
            doc = r.result["draft"]
            assert doc["machines"][WIN]["environment"]["subtype"] == "provider" and doc["machines"][MAC]["environment"]["subtype"] == "own_harness"
            assert doc["machines"][WIN]["environment"]["option_id"] == "cli-claude" and doc["machines"][MAC]["environment"]["option_id"] == MAC_MODEL
            t = tags(r)
            assert t["MODEL"] >= 5 and t["SCRIPT"] == 0 and t["CLAUDE-CODE"] == 0 and t["OWNER"] == 1  # the one OWNER event is the press that started the run
            assert c.runs.drafts.current()["status"] == "proposed" and c.runs.drafts.approved() is None  # proposed, not approved: nothing is an output yet
    run(go())


def test_the_model_cannot_close_the_draft_before_every_machine_and_the_team_are_drafted(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), win_provider(), finish(), act(action="advance", to="done"), team(), finish()])
            assert r.status != "done" and "finish_draft" not in r.summary["tools_run"]  # finish_draft was never offered: the evidence of its exit was not in
            assert any(e.type in ("protocol_refused", "action_rejected") for e in r.store.events())
    run(go())


def test_with_no_stored_analysis_the_protocol_ends_as_no_machines_and_drafts_nothing(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network")], network=lambda: [{"node_id": WIN, "name": "w", "stored": None}])
            assert r.status == "no_machines" and r.result["draft"] is None and c.runs.drafts.current() is None
    run(go())


# ---------------------------------------------------------------------------------------------------------------- hard limits (GUARD)

def check(f, mid, **kw):
    s = {"id": mid, "name": mid}
    base = dict(subtype="provider", access="subscription:claude", model="none", resources=["plan"], sensitivity="internal")
    base.update(kw)
    return D.machine_errors(s, f, NOCON, **base)


def rules(errs):
    return {e["rule"] for e in errs}


def test_an_access_that_was_not_detected_is_refused_and_one_that_was_is_checked():
    errs, checks = check(WINF, WIN, access="api:openai")
    assert "access_not_detected" in rules(errs) and checks == []
    errs, checks = check(WINF, WIN)
    assert errs == [] and checks[0]["rule"] == "access_present" and checks[0]["fact"] == "machine.ai_clis"


def test_a_tool_without_a_sign_in_found_is_not_an_access():
    f = {**WINF, "machine.ai_clis": [{**WINF["machine.ai_clis"][0], "set_up": False}]}
    assert "access_not_detected" in rules(check(f, WIN)[0])


def test_an_api_key_counts_only_when_its_variable_was_found_and_never_by_value():
    f = {**WINF, "machine.api_key_variable_names": [{"name": "SOME_KEY_VARIABLE", "provider": "openai"}]}
    ids = [a["id"] for a in D.accesses(f)]
    assert "api:openai" in ids and "SOME_KEY_VARIABLE" not in json.dumps(D.accesses(f))
    assert check(f, WIN, subtype="provider", access="api:openai", resources=["api"])[0] == []


def test_a_confidential_machine_refuses_every_access_that_sends_data_out_and_keeps_the_local_one():
    assert "confidential_cloud" in rules(check(WINF, WIN, sensitivity="confidential")[0])
    kept, _ = D.fitting_local_options(MACF, "confidential")
    errs, _ = check(MACF, MAC, subtype="own_harness", access="local_hardware", model=MAC_MODEL, resources=["local_models"], sensitivity="confidential")
    assert errs == [] and MAC_MODEL in kept


def test_an_option_the_setup_cannot_do_yet_is_never_drafted_ollama_has_no_recipe():
    kept, removed = D.fitting_local_options(MACF, "public")
    assert "use-ollama" not in kept and "no_recipe_yet" in removed["use-ollama"]  # the draft offers only what the setup protocol can carry out
    errs, _ = check(MACF, MAC, subtype="own_harness", access="local_hardware", model="use-ollama", resources=["local_models"], sensitivity="public")
    assert errs and errs[0]["rule"] == "model_not_allowed" and "no_recipe_yet" in errs[0]["reason"]


def test_a_model_that_does_not_fit_the_measured_memory_is_refused_with_the_measured_numbers_and_one_that_fits_is_checked():
    tiny = {**MACF, "machine.model_path": "cpu", "machine.ram_available_gib": 2.0, "machine.ram_total_gib": 3.0}
    kept, removed = D.fitting_local_options(tiny, "internal")
    big = next(i for i, why in removed.items() if "GiB" in why and i.startswith("catalog-"))
    errs, _ = check(tiny, MAC, subtype="own_harness", access="local_hardware", model=big, resources=["local_models"])
    assert rules(errs) == {"model_not_allowed"} and "GiB" in errs[0]["reason"]
    ok = next(i for i in kept if i.startswith("catalog-"))
    errs, checks = check(tiny, MAC, subtype="own_harness", access="local_hardware", model=ok, resources=["local_models"])
    assert errs == [] and {"model_fits", "license_allowed"} <= {x["rule"] for x in checks}
    assert "unknown_model" in rules(check(tiny, MAC, subtype="own_harness", access="local_hardware", model="made-up-model", resources=["local_models"])[0])


def test_a_license_outside_the_allowed_classes_is_refused():
    cat = O.load_catalog()
    row = next(m for m in cat["model_files"] if m.get("license"))
    odd = {**cat, "model_files": [{**row, "license": "proprietary-xyz"}]}
    kept, removed = D.fitting_local_options(WINF, "internal", odd)
    hit = [i for i, why in removed.items() if "license" in why]
    assert hit and not any(i in kept for i in hit)
    errs, _ = D.machine_errors({"id": WIN, "name": WIN}, WINF, NOCON, subtype="own_harness", access="local_hardware", model=hit[0], resources=["local_models"], sensitivity="internal", catalog=odd)
    assert rules(errs) == {"model_not_allowed"} and "license" in errs[0]["reason"]


def test_what_the_owner_stated_for_a_machine_binds_and_what_the_profile_remembers_does_not():
    c = D.Constraints(resources={WIN: ["local_models"]})
    assert {"outside_owner_resources", "contradicts_owner"} <= rules(D.machine_errors({"id": WIN, "name": WIN}, WINF, c, subtype="provider", access="subscription:claude", model="none", resources=["plan"], sensitivity="internal")[0])


def test_no_combination_the_model_could_write_gets_past_the_limits_into_a_draft():
    """Every combination over both machines: whatever the guard lets through satisfies each hard limit; the rest is refused with a typed rule."""
    for (name, f), sub, sens, res in itertools.product((("w", WINF), ("m", MACF)), D.vocab()["subtypes"], D.vocab()["sensitivity"], (["plan"], ["local_models"], ["api"])):
        have = {a["id"]: a for a in D.accesses(f)}
        kept, _ = D.fitting_local_options(f, sens)
        for access in list(have) + ["api:nobody"]:
            for model in ("none", next(iter(kept)), "bogus"):
                errs, _ = D.machine_errors({"id": name, "name": name}, f, NOCON, subtype=sub, access=access, model=model, resources=res, sensitivity=sens)
                if not errs:
                    assert access in have and have[access]["kind"] in D.vocab()["subtypes"][sub]["access_kinds"]
                    assert not (have[access]["cloud"] and sens == "confidential")
                    assert (model in kept) if D.vocab()["subtypes"][sub]["needs_model"] else model in ("none", "")


def test_the_team_needs_machines_a_known_director_and_one_the_owner_did_not_leave_out():
    both = [WIN, MAC]
    assert "topology_needs_machines" in rules(D.network_errors("peers", [], [WIN], NOCON, freedom="propose_only", rhythm="owner_present"))
    assert "director_needed" in rules(D.network_errors("director_workers", [], both, NOCON, freedom="propose_only", rhythm="owner_present"))
    assert "director_not_applicable" in rules(D.network_errors("peers", [MAC], both, NOCON, freedom="propose_only", rhythm="owner_present"))
    assert "director_unknown" in rules(D.network_errors("director_workers", ["n-ghost"], both, NOCON, freedom="propose_only", rhythm="owner_present"))
    assert "chain_order_incomplete" in rules(D.network_errors("chain", [MAC], both, NOCON, freedom="propose_only", rhythm="owner_present"))
    assert "director_excluded" in rules(D.network_errors("director_workers", [MAC], both, D.Constraints(excluded={MAC}), freedom="propose_only", rhythm="owner_present"))
    assert D.network_errors("chain", [MAC, WIN], both, NOCON, freedom="propose_only", rhythm="owner_present") == []


def test_a_long_refusal_fits_the_result_limit_and_a_machine_is_named_only_by_its_listed_id(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), win_provider(choice="n-win/own_harness/local_hardware/nope"), act("apply_edit", args={"kind": "resources", "machine": "network.machine.n_win", "value": "local_models", "quote": "solo modelos locales"}),
                                act("apply_edit", args={"kind": "resources", "machine": "win-laptop", "value": "local_models", "quote": "solo modelos locales"})], correction=CORR)
            errs = errors(r)
            assert errs and all(len(e) <= 300 for e in errs) and "unknown_choice" in errs[0]
            assert sum("unknown_machine" in e and "n-win" in e for e in errs) == 2  # neither the fact key nor the name is a machine id
    run(go())


def test_a_refused_proposal_writes_no_fact_and_the_model_reads_the_typed_reason(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), win_provider(choice="n-win/provider/api:openai"), win_provider(choice="n-win/provider/subscription:claude/bogus"), mac_own(model="bogus")])
            errs = errors(r)
            assert len(errs) == 3 and all("unknown_choice" in e and "valid" in e for e in errs)
            assert "n-win/provider/subscription:claude" in errs[0] and MAC_MODEL in errs[2]  # the refusal lists the valid ids of that machine
            assert not [f for f in r.store.current_facts(r.target.id) if f.key.startswith("draft.machine.")]
    run(go())


# ---------------------------------------------------------------------------------------------------------------- assumptions

def test_every_element_is_the_owners_or_an_assumption_that_names_what_it_rests_on(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, GOOD)
            doc = r.result["draft"]
            known = {f.key for f in r.store.current_facts(r.target.id)}
            els = [e for m in doc["machines"].values() for e in m["elements"]] + doc["network"]["elements"]
            assert els and {e["status"] for e in els} <= {"owner", "assumption"}
            for e in els:
                if e["status"] == "assumption":
                    assert e["rests_on"] and all((ref[8:] in D.defaults()) if ref.startswith("default:") else ref in known for ref in e["rests_on"]), e
            assert {e["element"] for e in doc["network"]["elements"]} == {"tasks", "topology", "director", "freedom", "rhythm"}
            assert all(c_["ok"] for m in doc["machines"].values() for c_ in m["checks"]) and doc["machines"][MAC]["checks"]  # measured here, by code
    run(go())


def test_an_unmarked_assumption_and_a_basis_outside_the_closed_set_are_refused(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), win_provider(rests_on=[]), win_provider(rests_on=["default:made_up"]), win_provider(rests_on=["machine.invented"]), win_provider(rests_on="default:<id>"),
                                team(rests_on=[])])
            errs = errors(r)
            assert sum("assumption_unmarked" in e for e in errs) == 2 and sum("unresolved_basis" in e for e in errs) == 3
            assert all("valid values" in e for e in errs if "unresolved_basis" in e)
            assert not [f for f in r.store.current_facts(r.target.id) if f.key.startswith("draft.") and f.key != "draft.pending" and f.status.value != "unknown"]  # no proposal was written (the pending line is a record of the state)
    run(go())


# ---------------------------------------------------------------------------------------------------------------- the owner corrects by talking

CORR = "En la laptop solo modelos locales, y que el Mac dirija."


def test_the_correction_is_an_owner_event_and_the_operators_is_counted_apart(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network")], correction=CORR)
            ev = [e for e in r.store.events() if e.type == "owner_correction"]
            assert len(ev) == 1 and ev[0].effective_tag == "OWNER" and ev[0].payload["text"] == CORR
            assert any(f.key == "owner.correction.pending" and f.value["text"] == CORR for f in r.store.current_facts(r.target.id))
            r2 = await drive(c, [act("load_network")], correction=CORR, via="claude-code")
            e2 = [e for e in r2.store.events() if e.type == "owner_correction"][0]
            assert e2.effective_tag == "CLAUDE-CODE" and tags(r2)["CLAUDE-CODE"] >= 1
    run(go())


def edit(kind, machine="none", value="none", quote="solo modelos locales"):
    return act("apply_edit", args={"kind": kind, "machine": machine, "value": value, "quote": quote})


def test_an_edit_needs_the_owners_words_a_known_machine_and_a_closed_value(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), edit("resources", WIN, "local_models", quote="algo que el dueño no dijo"), edit("resources", "n-ghost", "local_models"), edit("resources", WIN, "magia"),
                                edit("nonsense", WIN, "x"), edit("resources", WIN, "local_models")], correction=CORR)
            errs = errors(r)
            assert any("quote_not_in_owner_words" in e for e in errs) and any("unknown_machine" in e for e in errs) and any("bad_value" in e for e in errs) and any("bad_edit_kind" in e for e in errs)
            f = [x for x in r.store.current_facts(r.target.id) if x.key.startswith("owner.edit.")]
            assert [x.key for x in f] == ["owner.edit.1"] and f[0].value["value"] == ["local_models"] and f[0].value["quote"] == "solo modelos locales"
        async with coordinator(tmp_path / "b") as c:
            r = await drive(c, [act("load_network"), edit("resources", WIN, "local_models")])  # no correction this round
            assert any("no_owner_correction" in e for e in errors(r))
    run(go())


def test_an_edit_binds_the_rest_of_the_round_and_makes_an_earlier_proposal_stale(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), win_provider(), mac_own(), team(), edit("resources", WIN, "local_models"), finish(), win_provider(), win_local(), finish()], correction=CORR)
            assert any("unknown_choice" in e for e in errors(r))  # after the owner's edit the provider is no longer a valid id: the earlier proposal cannot be written again
            rej = [e for e in r.store.events() if e.type in ("action_rejected", "protocol_refused")]
            assert rej  # finish_draft was not on the menu while the invalidated proposal stood: the guard does not let it close
            assert r.status == "done"
            win = r.result["draft"]["machines"][WIN]
            res = [e for e in win["elements"] if e["element"] == "resources"][0]
            assert win["environment"]["subtype"] == "own_harness" and res["status"] == "owner" and res["source"] == "owner.edit.1"
    run(go())


def test_leaving_a_machine_out_removes_it_from_the_coverage_and_it_cannot_direct(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), edit("exclude_machine", MAC, quote="el Mac"), mac_own(), team(), win_provider(), team(team="single/n-win", rests_on=["default:single_machine", "default:no_task_info", "default:ask_before_acting", "default:owner_present"]), finish()],
                            correction="Deja fuera el Mac, por favor.")
            errs = errors(r)
            assert sum("unknown_choice" in e for e in errs) == 2  # the machine the owner left out has no valid choice and cannot direct
            assert r.status == "done" and list(r.result["draft"]["machines"]) == [WIN] and r.result["draft"]["excluded"] == [MAC]
    run(go())


# ---------------------------------------------------------------------------------------------------------------- asking, only closed

def test_ask_owner_offers_closed_questions_shows_the_machines_unordered_with_facts_and_never_a_best(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            m = ScriptedModel([act("load_network"), act("ask_owner", args={"question": "director_machine"}), act("ask_owner", args={"question": "made_up"})])
            r = c.runs.start_draft("web", model=m, status=AVAILABLE, network=two())
            for _ in range(500):
                if r.adapter and r.adapter.pending:
                    break
                await asyncio.sleep(0.02)
            p = r.adapter.pending
            assert p.qid == "director_machine" and [o["id"] for o in p.options] == sorted([WIN, MAC])  # by id: not ranked
            assert all("RAM" in o["detail"] and "accesses" not in o for o in p.options) and not any(k in json.dumps(p.options).lower() for k in ("best", "recommend", "mejor"))
            assert not c.answer_run(r.id, "director_machine", "n-ghost")  # a wrong option is not taken
            assert c.answer_run(r.id, "director_machine", MAC)
            await asyncio.wait_for(r.task, 30)
            ev = [e for e in r.store.events() if e.type == "owner_answer"]
            assert len(ev) == 1 and ev[0].effective_tag == "OWNER" and any("unknown question" in e for e in errors(r))
            assert {f.key: f.value for f in r.store.current_facts(r.target.id)}["owner.answer.director_machine"] == MAC
    run(go())


def test_an_answer_binds_the_draft_and_makes_the_team_element_the_owners(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), act("ask_owner", args={"question": "work_sensitivity"}), win_provider(), mac_own(), team(), finish()], answers={"work_sensitivity": "confidential"})
            errs = errors(r)
            assert any("unknown_choice" in e and "n-win/provider" not in e.split("valid")[-1] for e in errs)  # after the owner said confidential the provider is no longer a valid choice
    run(go())


# ---------------------------------------------------------------------------------------------------------------- profile and output

def test_an_approval_yields_picks_a_topology_and_a_profile_that_the_next_draft_sees_without_being_bound(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), edit("resources", WIN, "local_models"), edit("director", MAC, quote="que el Mac dirija"), win_local(), mac_own(),
                                team(), finish()], correction=CORR)
            assert r.status == "done", errors(r)
            assert c.runs.approve_draft("nope") is None
            out = c.runs.approve_draft(r.id)
            assert out
            pick = {p["machine_id"]: p for p in out["picks"]}
            assert pick[MAC]["option_id"] == MAC_MODEL and pick[WIN]["option_id"].startswith("catalog-") and all(p["executed"] is False and p["sensitivity"] for p in out["picks"])
            assert out["topology"]["roles"] == {MAC: "director", WIN: "worker"} and out["topology"]["status"] == D.PROVISIONAL
            assert c.runs.drafts.approved()["picks"] == out["picks"] and c.runs.drafts.current()["status"] == "approved"
            prof = c.runs.profile_view()["entries"]
            assert {e["kind"] for e in prof} == {"resources", "director"} and next(e for e in prof if e["kind"] == "resources")["value"] == ["local_models"] and all(e["quote"] for e in prof)
            assert [e for e in r.store.events() if e.type == "owner_approve_draft"][0].effective_tag == "OWNER"
            # next round from scratch: the profile is in the view as context, and a draft that ignores it is accepted (no code forces it)
            r2 = await drive(c, GOOD)
            keys = {f.key for f in r2.store.current_facts(r2.target.id)}
            assert {"owner.profile.p1", "owner.profile.p2"} <= keys and r2.status == "done"
            assert r2.result["draft"]["machines"][WIN]["environment"]["subtype"] == "provider"
            # the owner resets it: gone from the next view, and the reset is an owner action in the log
            assert c.runs.profile_reset() == 2 and c.runs.profile_view()["entries"] == []
            assert c.log.events("owner_profile_reset")
            r3 = await drive(c, [act("load_network")])
            assert not [f for f in r3.store.current_facts(r3.target.id) if f.key.startswith("owner.profile.")]
    run(go())


def test_only_the_current_proposed_draft_can_be_approved_and_only_once(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            a = await drive(c, GOOD)
            b = await drive(c, GOOD)
            assert c.runs.approve_draft(a.id) is None  # an older round
            assert c.runs.approve_draft(b.id) is not None and c.runs.approve_draft(b.id) is None
            assert b.result["approved"] is True
    run(go())


def test_the_output_of_an_embedded_environment_installs_nothing_and_a_chain_lists_its_links():
    doc = {"round": 1, "machines": {"a": {"environment": {"subtype": "embedded", "access": "app_license:x", "option_id": None}, "sensitivity": {"value": "internal"}},
                                    "b": {"environment": {"subtype": "provider", "access": "api:openai", "option_id": "api-openai"}, "sensitivity": {"value": "confidential"}}},
           "network": {"topology": "chain", "director": ["b", "a"], "freedom": "propose_only", "rhythm": "owner_present", "tasks": "t"}}
    out = D.approved_output(doc)
    assert out["picks"][0]["option_id"] is None and "nothing to install" in out["picks"][0]["note"] and out["topology"]["roles"] == {"b": "link 1", "a": "link 2"}


# ---------------------------------------------------------------------------------------------------------------- intents and the harness rules

def test_the_run_view_emits_intents_with_a_english_text_fallback(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, GOOD)
            intents = c.runs.view(r.id)["intents"]
            show = [i for i in intents if i["kind"] == "show_data"][0]
            assert set(show) == {"kind", "run_id", "actor", "sensitivity", "payload", "text_fallback"} and show["run_id"] == r.id and show["payload"]["card"] == "draft"
            assert show["text_fallback"].startswith("Draft of your network (round 1)") and "assumption" in show["text_fallback"] and show["text_fallback"].isascii() and show["sensitivity"] == "internal"
            ask = D.ask_choice_intent({"question": "freedom", "text": "How much freedom?", "options": D.questions()["freedom"]["options"]}, r.id)
            assert ask["kind"] == "ask_choice" and "1. It only proposes" in ask["text_fallback"] and ask["payload"]["options"][0]["id"] == "propose_only"
    run(go())


def test_invalid_actions_end_the_run_as_blocked_and_no_code_path_moves_the_protocol_for_the_model(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("rm_rf"), act("node_os_details"), act("finish_draft", args={"summary": "x"}), act("draft_network"), act("sudo"), act("load_network"), act("sudo")])
            assert r.status != "done" and "finish_draft" not in r.summary["tools_run"] and "draft_network" not in r.summary["tools_run"]
            assert not [e for e in r.store.events() if e.type == "protocol_state" and e.payload.get("to") in ("done", "no_machines")]
            assert tags(r)["SCRIPT"] == 0
    run(go())


def test_the_protocol_is_the_one_the_skill_declares_and_the_menu_fits_the_small_profile():
    from envrec.protocols import ProtocolSet
    from envrec.skills.discover import library
    from envrec_coordinator.draft_agent import DraftAdapter
    p = ProtocolSet.from_library(library()).for_kind("network")
    assert p.meta.protocol == "draft" and set(p.meta.states.states) == {"draft", "done", "no_machines"}
    names = [t.name for t in DraftAdapter(network=lambda: [], store=None, owner_events=None).tool_specs()]
    assert len(names) <= 6 and set(names) == set(p.meta.states.states["draft"].tools)


def test_the_draft_code_is_os_neutral_a_synthetic_linux_dict_goes_through_the_same_functions():
    """SYNTHETIC (hand-built, NOT a capture; Linux is UNTESTED against a real machine): the guards read facts, not OS names."""
    f = {**MACF, "machine.os.name": "Linux", "machine.os.family": "linux", "machine.os.arch": "x86_64", "machine.model_path": "gpu", "machine.vram_gib": 12.0, "machine.gpus": [{"vendor": "NVIDIA", "name": "x", "vram_gib": 12.0, "memory": "dedicated"}],
         "machine.ai_clis": [], "machine.runtimes": []}
    assert [a["id"] for a in D.accesses(f)] == ["local_hardware"]
    assert D.machine_summary("n-lin", "linux-box", {"facts": {k: {"value": v} for k, v in f.items()}})["os"] == "Linux"
    kept, _ = D.fitting_local_options(f, "internal")
    assert kept and D.machine_errors({"id": "n-lin", "name": "l"}, f, NOCON, subtype="own_harness", access="local_hardware", model=next(iter(kept)), resources=["local_models"], sensitivity="internal")[0] == []


def test_no_per_device_branch_in_the_draft_code():
    src = (ROOT / "src" / "envrec_coordinator" / "draft.py").read_text() + (ROOT / "src" / "envrec_coordinator" / "draft_agent.py").read_text()
    for word in ("Windows", "macOS", "Darwin", "Lenovo", "win32", "sys.platform", "platform.system"):
        assert word not in src, word


# ---------------------------------------------------------------------------------------------------------------- the closed sets

def test_the_closed_sets_hold_only_what_passes_the_limits_and_shrink_when_the_owner_states_something():
    for mid, f in ((WIN, WINF), (MAC, MACF)):
        s = {"id": mid, "name": mid}
        for sens in D.vocab()["sensitivity"]:
            c = D.Constraints(work_sensitivity=sens)
            ch = D.machine_choices(mid, s, f, c)["choices"]
            assert ch
            for cid in ch:
                m, sub, acc, model = D.parse_choice(cid)
                assert m == mid and D.machine_errors(s, f, c, subtype=sub, access=acc, model=model, resources=[D.IMPLIED_RESOURCE[next(a for a in D.accesses(f) if a["id"] == acc)["kind"]]], sensitivity=sens)[0] == []
                if sens == "confidential":
                    assert not next(a for a in D.accesses(f) if a["id"] == acc)["cloud"]
        assert any("/provider/" in x for x in D.machine_choices(mid, s, f, NOCON)["choices"])
        only_local = D.machine_choices(mid, s, f, D.Constraints(resources={mid: ["local_models"]}))["choices"]
        assert only_local and all("/own_harness/" in x for x in only_local)  # what the owner said removes the other ids
    assert D.team_choices([WIN, MAC], NOCON) == ["single/n-mac", "single/n-win", "director_workers/n-mac", "director_workers/n-win", "peers", "chain/n-mac,n-win", "chain/n-win,n-mac"]
    assert D.team_choices([WIN, MAC], D.Constraints(excluded={MAC})) == ["single/n-win"] and "peers" not in D.team_choices([WIN, MAC], D.Constraints(topology="chain"))


def test_the_menu_shows_the_valid_ids_as_values_and_no_placeholder_anywhere_the_model_can_copy(tmp_path):
    from envrec.loop.menu import render_menu, MenuEntry

    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), win_provider()])
            ad = r.adapter
            specs = {t.name: t for t in ad.tool_specs()}
            props = specs["draft_machine"].input_model["properties"]
            assert props["choice"]["enum"] == ad._all_choices() and "n-mac/own_harness/local_hardware/" + MAC_MODEL in props["choice"]["enum"]
            assert props["rests_on"]["items"]["enum"] == D.valid_refs({f.key for f in r.store.current_facts(r.target.id)}) and set(specs["apply_edit"].input_model["properties"]["machine"]["enum"]) == {WIN, MAC, "none"}
            text = render_menu([MenuEntry(t.name, t.one_line, t, ad) for t in specs.values()])
            assert "values: " in text and " n-win/provider/subscription:claude" in text and "<id>" not in text and "<" not in text.replace("<-", "")
            blob = json.dumps([t.input_model for t in specs.values()]) + (ROOT / "skills" / "protocol-draft" / "SKILL.md").read_text() + json.dumps(D.defaults()) + json.dumps(D.vocab())
            assert "<id>" not in blob and "default:<" not in blob
    run(go())


def test_everything_the_owner_reads_from_the_draft_data_is_english():
    blob = json.dumps(D.defaults(), ensure_ascii=False) + json.dumps(D.questions(), ensure_ascii=False) + json.dumps(D.vocab(), ensure_ascii=False)
    from envrec_coordinator import draft_strings as S
    blob += "".join(v for k, v in vars(S).items() if k.isupper() and isinstance(v, str))
    assert blob.isascii()


# ---------------------------------------------------------------------------------------------------------------- what is still to draft

def test_the_still_to_draft_line_shrinks_as_machines_and_the_team_get_drafted_for_any_number_of_machines(tmp_path):
    three = lambda: [{"node_id": i, "name": n, "stored": st} for i, n, st in (
        (WIN, "win-laptop", stored("windows-real/typed_facts.redacted.json", WIN)), (MAC, "mac-coordinator", stored("darwin-real/typed_facts.json", MAC)),
        ("n-mac2", "mac-second", stored("darwin-real/typed_facts.json", "n-mac2")))]
    seen: list[dict] = []
    holder: dict = {}

    def snap(reply):
        def f(state, user):
            r = holder["run"]
            pend = [x for x in r.store.current_facts(r.target.id) if x.key == "draft.pending"]
            seen.append(pend[0].value if pend else None)
            return reply
        return f

    mac2 = act("draft_machine", args={"choice": "n-mac2/own_harness/local_hardware/" + MAC_MODEL, "resources": ["local_models"], "because": "Same stack as the first Mac.", "rests_on": REFS_MAC})
    script = [snap(act("load_network")), snap(win_provider()), snap(mac_own()), snap(mac2), snap(team(team="peers")), snap(finish())]

    async def go():
        async with coordinator(tmp_path) as c:
            r = c.runs.start_draft("web", model=ScriptedModel(script), status=AVAILABLE, network=three)
            holder["run"] = r
            await asyncio.wait_for(r.task, 60)
            assert r.status == "done", errors(r)
            last = [x for x in r.store.current_facts(r.target.id) if x.key == "draft.pending"][0]
            assert last.value == {"machines_without_draft": [], "team_drafted": True, "still_needed_to_end": []} and last.created_by.value == "loop"
            assert "draft.pending" in D_SHOW(r)
    run(go())
    assert seen[0] is None  # before load_network there is nothing to say
    assert [x["machines_without_draft"] for x in seen[1:]] == [["n-mac", "n-mac2", "n-win"], ["n-mac", "n-mac2"], ["n-mac2"], [], []]  # one machine fewer after each draft
    assert [x["still_needed_to_end"] for x in seen[1:]][0] == ["a draft of machine n-mac", "a draft of machine n-mac2", "a draft of machine n-win", "a draft of the team"]
    assert seen[4]["still_needed_to_end"] == ["a draft of the team"] and seen[4]["team_drafted"] is False and seen[5]["still_needed_to_end"] == []


def D_SHOW(r) -> list[str]:
    from envrec.protocols import ProtocolSet
    from envrec.skills.discover import library
    return ProtocolSet.from_library(library()).for_kind("network").meta.states.states["draft"].show


def test_the_still_to_draft_line_follows_what_the_owner_leaves_out_and_names_no_action(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), win_provider(), edit("exclude_machine", MAC, quote="el Mac")], correction="Deja fuera el Mac, por favor.")
            p = [x for x in r.store.current_facts(r.target.id) if x.key == "draft.pending"][0].value
            assert p == {"machines_without_draft": [], "team_drafted": False, "still_needed_to_end": ["a draft of the team"]}
            text = json.dumps(p).lower()
            assert not any(t in text for t in ("draft_machine", "draft_network", "finish_draft", "should", "next", "call"))  # a record of the state: no tool named, no advice
    run(go())


def test_the_model_can_read_the_still_to_draft_line_and_the_valid_choices_in_its_view(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r = await drive(c, [act("load_network"), win_provider(), act("load_network")])
            views = [e.payload["view"] for e in r.store.events() if e.type == "view_built"]
            last = views[2]  # after the laptop was drafted
            assert "draft.pending = machines_without_draft: [" in last and '"n-mac"' in last and '"n-win"' not in last.split("draft.pending")[1].split("\n")[0]
            assert "team_drafted: false" in last and "a draft of machine n-mac" in last and "a draft of the team" in last
            assert "valid_choices: [" in last and "n-mac/provider/subscription:claude" in last
    run(go())
