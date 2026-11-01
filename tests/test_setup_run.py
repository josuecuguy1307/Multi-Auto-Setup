"""The setup protocol end to end: install -> minimal call -> undo -> reinstall, on this machine. SIMULATED-LOCAL: a coordinator and a real node agent talk over real TLS on loopback; the model is scripted; the
runtime and the model file come from a web server on loopback (pinned in a test catalog by their real size and SHA-256); the runtime is a small program with the real server's flags and endpoints. What is NOT
simulated: the node's operations (download, check, unpack, the detached server on 127.0.0.1, the call, the stop), the per-OS layer, the signed write tasks and the owner's approval of each step bound to the plan hash.
The machine's cell (and so the build) is whatever this machine is. Skipped on Windows, where the test runtime cannot run (UNTESTED there)."""
from __future__ import annotations

import asyncio
import copy
import json
import socket
import time

import httpx
import pytest

import envrec_os
from envrec.model import ScriptedModel
from envrec_coordinator import options as O
from envrec_coordinator.setup_plan import load_recipes
from envrec_os import procs
from test_executors import Origin, fake_runtime, free_port, sha
from test_mas_agent import AVAILABLE, act, analysed, down, node_up
from mas_world import coordinator

pytestmark = pytest.mark.skipif(envrec_os.family() == "windows", reason="the test runtime is a POSIX program (the Windows branch is UNTESTED)")
REAL_CATALOG = O.load_catalog()
LOOSE = O.load_rules().with_params(overhead_fixed_gib=0.0, overhead_weight_share=0.0)  # the test model is a few bytes: the machine's real memory is read, the runtime allowance is not charged


def run(coro):
    return asyncio.run(coro)


def make_world(origin_factory, facts):
    """A test catalog for THIS machine's cell: the real asset and model rows with the address, size and SHA-256 of what the test origin serves; and a recipe that downloads from that origin."""
    sel = O.local_build(facts, REAL_CATALOG, LOOSE)
    assert sel["ok"], sel
    rt, model = fake_runtime(), b"not a real model, just bytes"
    origin = origin_factory({"/rt": rt, "/model": model})
    asset = {**sel["asset"], "url": origin.url("/rt"), "sha256": sha(rt), "bytes": len(rt)}
    row = next(r for r in REAL_CATALOG["model_files"] if r["kind"] == "chat" and r["offer_by_default"] and not r.get("shards") and not r.get("companions") and r["params_b"] < 4)
    row = {**row, "url": origin.url("/model"), "sha256": sha(model), "bytes": len(model), "dims": {}, "min_ram_gib": 0.01}
    catalog = {**REAL_CATALOG, "runtime_assets": [asset], "model_files": [row]}
    recipe = copy.deepcopy(load_recipes()["local-server"])
    recipe["download"] = {"schemes": ["http"], "hosts": {"127.0.0.1": ["127.0.0.1"]}, "max_redirects": 3, "disk_margin": 1.0}
    recipe["server"].update({"port": free_port(), "ready_timeout_s": 30, "call_timeout_s": 15})
    return catalog, {"local-server": recipe}


def record_pick(c, nid, catalog):
    facts = {k: row["value"] for k, row in c.runs.stored_analysis(nid)["facts"].items()}
    big = {**facts, "machine.ram_available_gib": 64.0, "machine.ram_total_gib": 64.0}  # only to BUILD the option that the owner picked; the install run reads the machine's real memory itself
    kept, _ = O.apply_filters(O.candidates(big, catalog, O.load_harnesses(), rules=LOOSE), big, "local", "internal", rules=LOOSE, catalog=catalog)
    opt = next(o for o in O.order_options(kept) if o["origin"] == "catalog_runtime")  # a machine that already has a runtime installed lists it first; the pick here is the downloadable one
    c.runs.picks.mkdir(parents=True, exist_ok=True)
    (c.runs.picks / f"{nid}.json").write_text(json.dumps({"run": "options-test", "node": nid, "picked_at": time.time(), "option": opt}, default=str))
    return opt, facts


def owner_approves(c, r, by="owner", stop=None):
    async def loop():
        seen = []
        while not r.task.done():
            p = r.adapter.pending
            if p is not None and p.qid.startswith("approve_") and p.qid not in seen[-1:]:
                seen.append(p.qid)
                assert c.answer_run(r.id, p.qid, "approve", by, None, "panel")
            await asyncio.sleep(0.02)
        return seen
    return asyncio.ensure_future(loop())


def tree(home):
    return sorted(str(p.relative_to(home)) for p in home.rglob("*")) if home.exists() else []


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_the_whole_setup_install_minimal_call_undo_reinstall_with_the_owner_approving_every_step_of_the_exact_plan(tmp_path):
    async def go():
        origins = []
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            facts = {k: row["value"] for k, row in c.runs.stored_analysis(nid)["facts"].items()}
            catalog, recipes = make_world(lambda f: origins.append(Origin(f)) or origins[-1], facts)
            record_pick(c, nid, catalog)
            agent.runner.ctx.update({"recipes": recipes})
            home, port = agent.state.dir / "mas", recipes["local-server"]["server"]["port"]
            snap: dict = {}

            def installed(s, u):
                info = json.loads((home / "run" / "server.json").read_text())
                snap["installed"] = {"files": tree(home), "pid": info["pid"], "created": info["created"], "alive": procs.is_ours(info["pid"], info["created"]), "health": httpx.get(f"http://127.0.0.1:{port}/health").status_code}
                return act("undo_step", args={"step": "6"})

            def clean(s, u):
                snap["clean"] = {"tree": tree(home), "alive": procs.is_ours(snap["installed"]["pid"], snap["installed"]["created"]), "port_open": port_open(port)}
                return act("run_step", args={"step": "1"})
            script = ([act("load_context"), act("recheck_fit"), act("build_plan")] + [act("run_step", args={"step": str(n)}) for n in range(1, 7)] + [installed]
                      + [act("undo_step", args={"step": str(n)}) for n in range(5, 0, -1)] + [clean] + [act("run_step", args={"step": str(n)}) for n in range(2, 7)]
                      + [act("finish_setup", args={"summary": "installed, answered, undone, installed again"})])
            r = c.runs.start_install(nid, "web", model=ScriptedModel(script), status=AVAILABLE, recipes=recipes, catalog=catalog, rules=LOOSE)
            assert r.task is not None, (r.status, r.reason)
            approver = owner_approves(c, r)
            await asyncio.wait_for(r.task, 120)
            await approver
            assert r.status == "done", (r.status, r.reason, r.summary, [(e.type, e.payload.get("tool") or e.payload.get("action") or e.payload.get("reason"), e.payload.get("error") or e.payload.get("ok")) for e in r.store.events() if e.type in ("action_chosen", "tool_result", "action_rejected")][-14:])

            assert snap["installed"]["alive"] and snap["installed"]["health"] == 200 and any(f.startswith("runtime") for f in snap["installed"]["files"]) and any(f.startswith("config") for f in snap["installed"]["files"])
            assert snap["clean"] == {"tree": [], "alive": False, "port_open": False}  # after the undo: not one file or folder, the process gone, the port closed

            facts_now = {f.key: f.value for f in r.store.current_facts(r.target.id)}
            call = facts_now["setup.minimal_call"]
            assert facts_now["setup.verified"] is True and call["ok"] is True and call["answer"] == "ready" and call["tested"] == "text" and call["vision"] == "untested" and isinstance(call["latency_ms"], int) and call["endpoint"] == f"127.0.0.1:{port}"
            assert facts_now["setup.server"]["bound_loopback"] in (True, None) and all(a.startswith("127.0.0.1:") for a in facts_now["setup.server"]["listening"])
            tested = [f for f in r.store.current_facts(r.target.id) if f.key == "setup.minimal_call"][0]
            assert tested.status.value == "tested"  # the answer and its time are evidence, not the model's word

            counts = c.runs.actor_counts(r)
            assert counts["MODEL"] == 44 and counts["OWNER"] == 19 and counts["CLAUDE-CODE"] == 0 and counts["SCRIPT"] == 0 and counts["GUARD"] > 0, counts  # 22 chosen actions; the owner pressed start and approved 18 steps
            approvals = [e.payload for e in c.log.events("setup_step_approved")]
            assert len(approvals) == 18 and {(a["step"], a["action"]) for a in approvals} == {(n, a) for n in range(1, 7) for a in ("run", "undo")} and len({a["plan_hash"] for a in approvals}) == 1
            assert all(e.effective_tag == "OWNER" for e in c.log.events("setup_step_approved"))
            writes = [t for t in c.tasks.list() if t.task_type == "setup_step"]
            assert len(writes) == 18 and all(t.status == "done" for t in writes), [(t.status, t.reason) for t in writes if t.status != "done"]
            assert (home / "run" / "server.json").is_file()  # installed again at the end
            await c.runs.shutdown()
            info = json.loads((home / "run" / "server.json").read_text())
            procs.stop(info["pid"], info["created"], grace_s=2)
            await down(agent, task)
        for o in origins:
            o.close()
    run(go())


def test_an_approval_that_is_not_the_owners_is_not_an_approval_and_nothing_is_written(tmp_path):
    async def go():
        origins = []
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            facts = {k: row["value"] for k, row in c.runs.stored_analysis(nid)["facts"].items()}
            catalog, recipes = make_world(lambda f: origins.append(Origin(f)) or origins[-1], facts)
            record_pick(c, nid, catalog)
            agent.runner.ctx.update({"recipes": recipes})
            script = [act("load_context"), act("recheck_fit"), act("build_plan"), act("run_step", args={"step": "1"})] + [act("recheck_fit")] * 8
            r = c.runs.start_install(nid, "web", model=ScriptedModel(script), status=AVAILABLE, recipes=recipes, catalog=catalog, rules=LOOSE)
            assert r.task is not None, (r.status, r.reason)
            approver = owner_approves(c, r, by="claude-code")  # the operator presses approve for the owner
            await asyncio.wait_for(r.task, 60)
            await approver
            assert r.status != "done"
            [wt] = [t for t in c.tasks.list() if t.task_type == "setup_step"]
            assert (wt.status, wt.reason) == ("rejected", "no_owner_approval")  # the desk looked for the OWNER's approval of this plan hash, step and action and found only the operator's
            assert any("no_owner_approval" in str(e.payload.get("error", "")) for e in r.store.events() if e.type == "tool_result")  # and the model was told why
            assert not (agent.state.dir / "mas").exists() or tree(agent.state.dir / "mas") == []  # nothing was downloaded
            ev = [e for e in c.log.events("setup_step_approved")]
            assert len(ev) == 1 and ev[0].effective_tag == "CLAUDE-CODE"  # recorded as the operator's, and the task desk never counted it
            assert c.runs.actor_counts(r)["CLAUDE-CODE"] >= 1
            await down(agent, task)
        for o in origins:
            o.close()
    run(go())


def test_a_machine_whose_cell_has_no_build_ends_as_cannot_install_with_the_typed_reason_and_changes_nothing(tmp_path):
    async def go():
        origins = []
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            facts = {k: row["value"] for k, row in c.runs.stored_analysis(nid)["facts"].items()}
            catalog, recipes = make_world(lambda f: origins.append(Origin(f)) or origins[-1], facts)
            record_pick(c, nid, catalog)
            r = c.runs.start_install(nid, "web", model=ScriptedModel([act("load_context"), act("recheck_fit"), act("build_plan")]), status=AVAILABLE, recipes=recipes, catalog={**catalog, "runtime_assets": []}, rules=LOOSE)
            await asyncio.wait_for(r.task, 60)
            assert r.status == "cannot_install", (r.status, r.reason)
            refused = {f.key: f.value for f in r.store.current_facts(r.target.id)}["setup.refused"]
            assert refused["reason"] == "no_build_in_catalog" and not [t for t in c.tasks.list() if t.task_type == "setup_step"]
            await down(agent, task)
        for o in origins:
            o.close()
    run(go())


def test_a_setup_does_not_start_without_a_stored_analysis_and_the_owners_pick_and_asks_no_model(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            model = ScriptedModel([act("load_context")])
            r = c.runs.start_install(nid, "web", model=model, status=AVAILABLE)
            assert r.status == "no_analysis" and r.task is None and model.calls == []
            await analysed(c, nid)
            r = c.runs.start_install(nid, "web", model=model, status=AVAILABLE)
            assert r.status == "no_pick" and "picked" in r.reason and r.task is None and model.calls == []
            assert c.runs.start_install("n-nobody", "web").status == "failed"
            await down(agent, task)
    run(go())


def test_only_steps_really_run_or_undone_count_as_progress_so_repeating_reads_or_failing_still_stops_the_run(tmp_path):
    """A loop of identical read-only calls, and a loop of refused or failed step calls, add no fact and no step: the loop's own no-progress stop ends both."""
    async def go():
        origins = []
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            facts = {k: row["value"] for k, row in c.runs.stored_analysis(nid)["facts"].items()}
            catalog, recipes = make_world(lambda f: origins.append(Origin(f)) or origins[-1], facts)
            record_pick(c, nid, catalog)
            agent.runner.ctx.update({"recipes": recipes})
            reads = c.runs.start_install(nid, "web", model=ScriptedModel([act("load_context")] + [act("recheck_fit")] * 14), status=AVAILABLE, recipes=recipes, catalog=catalog, rules=LOOSE)
            await asyncio.wait_for(reads.task, 60)
            assert reads.status == "stopped" and reads.summary["stop_reason"] == "no_progress", (reads.status, reads.summary)
            assert not [t for t in c.tasks.list() if t.task_type == "setup_step"]  # and nothing was written
            out_of_order = [act("load_context"), act("recheck_fit"), act("build_plan")] + [act("run_step", args={"step": "3"})] * 14  # step 3 before steps 1 and 2: refused by the guard every time
            refused = c.runs.start_install(nid, "web", model=ScriptedModel(out_of_order), status=AVAILABLE, recipes=recipes, catalog=catalog, rules=LOOSE)
            await asyncio.wait_for(refused.task, 60)
            assert refused.status in ("stopped", "blocked_needs_human") and refused.summary["stop_reason"] in ("no_progress", "blocked_needs_human"), (refused.status, refused.summary)
            assert not [t for t in c.tasks.list() if t.task_type == "setup_step"] and not any(f.key.startswith("setup.action.") for f in refused.store.current_facts(refused.target.id))
            await down(agent, task)
        for o in origins:
            o.close()
    run(go())


def test_a_screen_can_drive_the_whole_setup_through_the_run_api_alone(tmp_path):
    """What the Harness App needs, only through the coordinator's front door (the methods behind /api/install/start, /api/run, /api/node/pick, /api/run/answer): start from the recorded pick, read the whole plan
    before the first approval, see each approval as a pending decision bound to the plan hash, watch step events, and ask for an undo as its own decision."""
    async def go():
        origins = []
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            assert c.pick_of(nid) is None  # nothing recorded yet
            facts = {k: row["value"] for k, row in c.runs.stored_analysis(nid)["facts"].items()}
            catalog, recipes = make_world(lambda f: origins.append(Origin(f)) or origins[-1], facts)
            opt, _ = record_pick(c, nid, catalog)
            pick = c.pick_of(nid)
            assert pick["option"] == opt["id"] and pick["build"]["cell"] and pick["model"] and "sha256" in pick["build"]
            agent.runner.ctx.update({"recipes": recipes})
            script = [act("load_context"), act("recheck_fit"), act("build_plan"), act("run_step", args={"step": "1"}), act("undo_step", args={"step": "1"})]
            m = ScriptedModel(script)
            view = c.start_install_run(nid, "web", model=m, status=AVAILABLE, recipes=recipes, catalog=catalog, rules=LOOSE)
            run_id = view["id"]
            assert view["kind"] == "install" and view["plan"] is None
            for _ in range(500):  # the first approval is a pending owner decision, and the WHOLE plan is already data on that same view (the plan is built before any step can be asked)
                v = c.run_view(run_id)
                if v["pending"]:
                    break
                await asyncio.sleep(0.02)
            plan = v["plan"]
            assert plan is not None and len(plan["steps"]) == 6 and plan["hash"].startswith("sha256:") and plan["total_download_bytes"] > 0
            assert all({"n", "op", "kind", "title", "command", "writes", "bytes", "undo", "status"} <= set(s) for s in plan["steps"]) and [s["status"] for s in plan["steps"]] == ["pending"] * 6
            assert plan["steps"][0]["writes"] and plan["fit"]["fits"] is True
            d = v["pending"]
            assert d["question"] == "approve_run_1" and d["data"]["kind"] == "approve_step" and d["data"]["plan_hash"] == plan["hash"] and (d["data"]["step"], d["data"]["of"], d["data"]["action"]) == (1, 6, "run")
            assert d["data"]["command"] == plan["steps"][0]["command"] and d["data"]["undo"] == plan["steps"][0]["undo"] and {o["id"] for o in d["options"]} == {"approve", "reject"}
            assert c.answer_run(run_id, d["question"], "approve", "owner", None, "panel")
            for _ in range(500):  # the undo is its own request, with its own pending decision
                v = c.run_view(run_id)
                if v["pending"] and v["pending"]["question"] == "approve_undo_1":
                    break
                await asyncio.sleep(0.02)
            u = v["pending"]
            assert u["data"]["action"] == "undo" and u["data"]["plan_hash"] == plan["hash"] and u["data"]["step"] == 1
            assert [s["status"] for s in v["plan"]["steps"]][0] == "done"
            assert c.answer_run(run_id, u["question"], "approve", "owner", None, "panel")
            await asyncio.wait_for(c.runs.runs[run_id].task, 60)
            v = c.run_view(run_id)
            ev = [e["text"] for e in v["events"]]
            states = [t for t in ev if t.startswith("step 1")]
            assert "the plan is ready: 6 steps" in " ".join(ev)
            run_states = [t.split(": ", 1)[1] for t in states if "(run)" in t]
            undo_states = [t.split(": ", 1)[1] for t in states if "(undo)" in t]
            assert run_states[0] == "waiting for owner" and run_states[1] == "approved" and run_states[-1] == "done"  # and "sent", "accepted", "running" in between, as far as the node let them be seen
            assert undo_states[0] == "waiting for owner" and undo_states[1] == "approved" and undo_states[-1] == "done"
            assert v["plan"]["steps"][0]["status"] == "undone" and v["actors"]["OWNER"] == 3  # the start press and two approvals
            await c.runs.shutdown()
            await down(agent, task)
        for o in origins:
            o.close()
    run(go())
