"""The one task that writes (docs/NETWORK_CONTRACT.md 3.9): a plan, its hash, the owner's approval of exactly that hash, and a node that refuses everything else. SIMULATED-LOCAL where a network is
involved (real TLS on loopback); the plans and recipes here are test data, not the shipped ones."""
from __future__ import annotations

import asyncio
import copy
import os

import pytest

from envrec_net import setup as P
from envrec_net import task as T
from envrec_net.identity import create
from envrec_node import setup as node_setup
from envrec_node import tasks as node_tasks
from mas_world import coordinator, events
from test_mas_tasks import agent_up, down, finished, make_runner

RECIPE = {"id": "demo", "ops": {"fetch_check": {"writes": ["<mas>/downloads/"]}, "unpack": {"writes": ["<mas>/runtime/demo/"]}}, "sequence": ["fetch_check", "unpack"], "repeat": []}
OWNER_EVENT = "evt_owner_1"


def plan_dict(**over) -> dict:
    d = {"plan_id": "p1", "machine_id": "node1", "option_id": "catalog-x", "sensitivity": "internal", "recipe_id": "demo", "recipe_sha256": P.recipe_sha256(RECIPE),
         "fit": {"model_bytes": 10, "ram_available_bytes": 100, "fits": True, "limited_by": ""},
         "steps": [{"n": 1, "title": "Download the model", "kind": "download", "op": "fetch_check", "args": {"url": "https://example.test/m.gguf", "sha256": "a" * 64},
                    "exact_command": "download https://example.test/m.gguf and check its SHA-256", "writes": ["<mas>/downloads/m.gguf"], "bytes": 10, "undo": "delete <mas>/downloads/m.gguf"},
                   {"n": 2, "title": "Unpack", "kind": "install", "op": "unpack", "args": {}, "exact_command": "unpack into <mas>/runtime/demo/", "writes": ["<mas>/runtime/demo/"], "bytes": 5,
                    "undo": "delete <mas>/runtime/demo/"}]}
    d.update(over)
    return P.seal(P.SetupPlan.model_validate(d)).model_dump(mode="json")


def token_for(coord, me, plan, step=1, action="run", *, task_id="t1", sid="S1", approval=OWNER_EVENT, scope_over=None):
    parsed = P.parse_plan(plan)
    sc = T.write_scope(P.plan_hash(parsed), parsed.recipe_id, step, action, parsed.step(step).writes, approval)
    if approval is None:
        sc.pop("approval")
    sc.update(scope_over or {})
    return T.issue_token(coord, task_id=task_id, node_id=me.node_id, sid=sid, tools=["setup_step"], scope=sc)


async def send_step(r, coord, me, plan, step=1, action="run", *, token="auto", task_id="t1", **kw):
    sent = []

    async def send(t, p):
        sent.append((t, p))
    tok = token_for(coord, me, plan, step, action, task_id=task_id, **kw) if token == "auto" else token
    await r.on_task({"task_id": task_id, "task_type": "setup_step", "params": {"plan": plan, "step": step, "action": action}, "token": tok}, send)
    await asyncio.sleep(0.1)
    return sent


def runner(tmp_path, recipes=None, executors=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    r, coord, me = make_runner(tmp_path)
    r.ctx.update({"recipes": {"demo": RECIPE} if recipes is None else recipes, "mas_home": tmp_path / "mas", "executors": executors if executors is not None else {}})
    (tmp_path / "mas").mkdir(parents=True, exist_ok=True)
    return r, coord, me


def write_file_executor(args, scope, ctx):
    target = scope.path("<mas>/downloads/m.gguf")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("model")
    return {"wrote": str(target.name)}, [{"provider": "test"}]


def run(coro):
    return asyncio.run(coro)


# ---- the plan and its hash ---------------------------------------------------------------------------------------------------------------------

def test_the_hash_covers_what_decides_what_happens_and_not_what_changes_while_it_runs():
    plan = P.parse_plan(plan_dict())
    h = P.plan_hash(plan)
    assert h.startswith("sha256:") and plan.plan_hash == h
    running = copy.deepcopy(plan.model_dump(mode="json"))
    running["steps"][0]["status"] = "done"
    running["verification"] = {"endpoint": "127.0.0.1:8080", "ok": True, "latency_ms": 40, "evidence": "tested"}
    assert P.plan_hash(P.parse_plan(running)) == h  # progress is not a change of plan
    for path, new in (("steps.0.exact_command", "something else"), ("steps.0.writes", ["<mas>/downloads/other.gguf"]), ("steps.0.args", {"url": "https://evil.test/m"}), ("steps.1.undo", "nothing"),
                      ("recipe_sha256", "b" * 64), ("option_id", "catalog-y"), ("machine_id", "node2"), ("sensitivity", "confidential")):
        d = copy.deepcopy(plan.model_dump(mode="json"))
        cur = d
        *head, last = path.split(".")
        for k in head:
            cur = cur[int(k)] if k.isdigit() else cur[k]
        cur[int(last) if last.isdigit() else last] = new
        assert P.plan_hash(P.parse_plan(d)) != h, path
    swapped = plan.model_dump(mode="json")
    swapped["steps"] = [{**swapped["steps"][1], "n": 1}, {**swapped["steps"][0], "n": 2}]
    assert P.plan_hash(P.parse_plan(swapped)) != h  # the same steps in another order are another plan


@pytest.mark.parametrize("bad", ["/etc/passwd", "<mas>/../x", "<mas>/a/../../x", "C:\\Users\\x", "<mas>\\x", "~/x", "<mas>/", "<mas>", "<mas>//x", "other/x", "<mas>/a b", "<mas>/CON", "<mas>/runtime/nul.txt", "<mas>/Com3", "<mas>/lpt1.log", "<mas>/name.", "<mas>/" + "a" * 101])
def test_a_path_a_plan_may_write_is_mas_slash_plain_segments_and_nothing_else(bad):
    with pytest.raises(P.PlanError, match="bad_write_path"):
        P.clean_write_path(bad)
    d = plan_dict()
    d["steps"][0]["writes"] = [bad]
    with pytest.raises(Exception):
        P.parse_plan(d)


def test_a_plan_with_unnumbered_extra_or_oversized_content_is_not_a_plan():
    d = plan_dict()
    d["steps"][1]["n"] = 5
    with pytest.raises(P.PlanError, match="numbered"):
        P.parse_plan(d)
    d = plan_dict()
    d["command"] = "rm -rf /"
    with pytest.raises(P.PlanError, match="bad_plan"):
        P.parse_plan(d)
    d = plan_dict()
    d["steps"][0]["args"] = {"x": "y" * 70_000}
    with pytest.raises(P.PlanError, match="too large"):
        P.parse_plan(d)


# ---- the node: it runs a step only when everything checks, and says which check failed -------------------------------------------------------

def test_a_step_with_a_matching_owner_approval_runs_and_writes_only_inside_its_declared_path(tmp_path):
    async def go():
        r, coord, me = runner(tmp_path, executors={"fetch_check": write_file_executor})
        sent = await send_step(r, coord, me, plan_dict())
        assert [m[1].get("state") for m in sent][:2] == ["accepted", "running"] and sent[-1][1]["status"] == "done"
        assert sent[-1][1]["result"]["plan_hash"] == plan_dict()["plan_hash"] and sent[-1][1]["result"]["step"] == 1
        assert (tmp_path / "mas" / "downloads" / "m.gguf").read_text() == "model"
    run(go())


def _refusals(coord, me):
    """name -> (node kwargs, plan, step, token, the typed reason). Every one is a way to try to run a write that must end in a refusal before anything is accepted."""
    plan = plan_dict()
    approved = lambda p, **kw: token_for(coord, me, p, **kw)  # noqa: E731
    bad_apr = lambda **ap: approved(plan, scope_over={"approval": {"actor": "OWNER", "event": "e", "plan_hash": plan["plan_hash"], "step": 1, "action": "run", **ap}})  # noqa: E731
    altered = copy.deepcopy(plan)
    altered["steps"][0]["exact_command"] = "download something else"  # the hash it carries is no longer its own
    outside = plan_dict()
    outside["steps"][0]["writes"] = ["<mas>/elsewhere/m.gguf"]
    outside = P.seal(P.parse_plan(outside)).model_dump(mode="json")
    wrong_op = plan_dict()
    wrong_op["steps"][0]["op"] = "unpack"
    wrong_op = P.seal(P.parse_plan(wrong_op)).model_dump(mode="json")
    extra_op = plan_dict()
    extra_op["steps"][0]["op"] = "format_disk"
    extra_op = P.seal(P.parse_plan(extra_op)).model_dump(mode="json")
    other = plan_dict(option_id="catalog-OTHER")
    bare = T.issue_token(coord, task_id="t1", node_id=me.node_id, sid="S1", tools=["setup_step"])
    return {
        "no_scope": ({}, plan, 1, bare, "bad_scope"),
        "no_approval": ({}, plan, 1, approved(plan, approval=None), "no_owner_approval"),
        "approval_not_the_owners": ({}, plan, 1, bad_apr(actor="CLAUDE-CODE"), "no_owner_approval"),
        "approval_for_another_step": ({}, plan, 1, bad_apr(step=2), "no_owner_approval"),
        "approval_for_another_action": ({}, plan, 1, bad_apr(action="undo"), "no_owner_approval"),
        "token_made_for_another_plan": ({}, other, 1, approved(plan), "plan_altered"),
        "plan_altered_after_approval": ({}, altered, 1, approved(plan), "plan_altered"),
        "plan_that_was_never_sealed": ({}, {**plan, "plan_hash": ""}, 1, approved(plan), "plan_altered"),
        "scope_for_other_paths": ({}, plan, 1, approved(plan, scope_over={"writes": ["<mas>/downloads/other.gguf"]}), "bad_scope"),
        "unknown_recipe": ({"recipes": {}}, plan, 1, approved(plan), "unknown_recipe"),
        "stale_recipe": ({"recipes": {"demo": {**RECIPE, "ops": {**RECIPE["ops"], "x": {"writes": []}}}}}, plan, 1, approved(plan), "stale_recipe"),
        "path_outside_the_recipes_declared_writes": ({}, outside, 1, approved(outside), "outside_declared_writes"),
        "operation_that_is_not_the_recipes": ({}, wrong_op, 1, approved(wrong_op), "plan_not_from_recipe"),
        "an_operation_the_recipe_does_not_have": ({}, extra_op, 1, approved(extra_op), "plan_not_from_recipe"),
        "a_step_the_plan_does_not_have": ({}, plan, 3, bare, "bad_plan"),
        "not_a_plan": ({}, {"steps": "x"}, 1, bare, "bad_plan"),
    }


@pytest.mark.parametrize("name", ["no_scope", "no_approval", "approval_not_the_owners", "approval_for_another_step", "approval_for_another_action", "token_made_for_another_plan",
                                  "plan_altered_after_approval", "plan_that_was_never_sealed", "scope_for_other_paths", "unknown_recipe", "stale_recipe", "path_outside_the_recipes_declared_writes",
                                  "operation_that_is_not_the_recipes", "an_operation_the_recipe_does_not_have", "a_step_the_plan_does_not_have", "not_a_plan"])
def test_a_setup_step_is_refused_with_its_typed_reason_before_anything_is_accepted_or_written(tmp_path, name):
    async def go():
        calls = []
        r, coord, me = runner(tmp_path, executors={"fetch_check": lambda *a: calls.append(a) or ({}, []), "unpack": lambda *a: calls.append(a) or ({}, [])})
        kw, plan, step, token, reason = _refusals(coord, me)[name]
        r.ctx.update(kw)
        sent = await send_step(r, coord, me, plan, step, token=token)
        assert [(m[1]["state"], m[1]["reason"]) for m in sent] == [("rejected", reason)], sent
        assert calls == [] and r.running == 0 and not (tmp_path / "mas" / "downloads").exists()  # no function was called, nothing was written
    run(go())


def test_an_operation_cannot_write_outside_its_declared_paths_even_through_a_link(tmp_path):
    home = tmp_path / "mas"
    (home / "downloads").mkdir(parents=True)
    scope = node_setup.WriteScope(home, ["<mas>/downloads/m.gguf"])
    assert scope.path("<mas>/downloads/m.gguf") == (home / "downloads" / "m.gguf").resolve()
    for bad in ("<mas>/downloads/other", "<mas>/runtime/x", "<mas>/downloads/m.gguf/../../x"):
        with pytest.raises((node_setup.WriteRefused, P.PlanError)):
            scope.path(bad)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, home / "downloads" / "link")
    except (OSError, NotImplementedError):
        pytest.skip("this operating system or account may not create symbolic links (Windows without developer mode): the link case is not measurable here")
    with pytest.raises(node_setup.WriteRefused) as e:
        node_setup.WriteScope(home, ["<mas>/downloads/"]).path("<mas>/downloads/link/escape.txt")  # a link inside the folder that points out of it
    assert e.value.reason == "outside_declared_writes"


def test_an_operation_that_reaches_for_another_path_fails_the_step_and_leaves_nothing(tmp_path):
    def greedy(args, scope, ctx):
        scope.path("<mas>/downloads/m.gguf")
        scope.path("<mas>/secrets/key")  # not declared
        return {}, []

    async def go():
        r, coord, me = runner(tmp_path, executors={"fetch_check": greedy})
        sent = await send_step(r, coord, me, plan_dict())
        assert sent[-1][1]["status"] == "failed" and "outside_declared_writes" in sent[-1][1]["reason"]
        assert not (tmp_path / "mas" / "secrets").exists()
    run(go())


def test_a_node_without_a_function_for_the_operation_refuses_and_a_token_is_single_use(tmp_path):
    async def go():
        r, coord, me = runner(tmp_path)  # no executors
        sent = await send_step(r, coord, me, plan_dict())
        assert sent[-1][1]["status"] == "failed" and "no_executor" in sent[-1][1]["reason"]
        r2, coord2, me2 = runner(tmp_path / "b", executors={"fetch_check": write_file_executor})
        tok = token_for(coord2, me2, plan_dict())
        first = await send_step(r2, coord2, me2, plan_dict(), token=tok)
        again = await send_step(r2, coord2, me2, plan_dict(), token=tok)
        assert first[-1][1]["status"] == "done" and [(m[1]["state"], m[1]["reason"]) for m in again] == [("rejected", "token_reused")]
    run(go())


def test_the_undo_of_a_step_is_its_own_approved_action_and_runs_the_undo_operation(tmp_path):
    def undo(args, scope, ctx):
        scope.path("<mas>/downloads/m.gguf").unlink()
        return {"removed": True}, []

    async def go():
        r, coord, me = runner(tmp_path, executors={"fetch_check": write_file_executor, "undo_fetch_check": undo})
        await send_step(r, coord, me, plan_dict())
        assert (tmp_path / "mas" / "downloads" / "m.gguf").exists()
        refused = await send_step(r, coord, me, plan_dict(), action="undo", task_id="t2", scope_over={"approval": {"actor": "OWNER", "event": "e", "plan_hash": plan_dict()["plan_hash"], "step": 1, "action": "run"}})
        assert refused[-1][1]["reason"] == "no_owner_approval" and (tmp_path / "mas" / "downloads" / "m.gguf").exists()  # an approval to run is not an approval to undo
        done = await send_step(r, coord, me, plan_dict(), action="undo", task_id="t3")
        assert done[-1][1]["status"] == "done" and not (tmp_path / "mas" / "downloads" / "m.gguf").exists()
    run(go())


def test_the_read_only_types_still_refuse_every_write_shaped_parameter(tmp_path):
    async def go():
        r, coord, me = runner(tmp_path)
        for ttype, params in (("os_details", {"plan": plan_dict(), "step": 1}), ("node_report", {"writes": ["<mas>/x"]}), ("gpus", {"action": "run"}), ("memory_for_models", {"scope": {"recipe": "demo"}})):
            sent = []

            async def send(t, p):
                sent.append((t, p))
            await r.on_task({"task_id": "t-" + ttype, "task_type": ttype, "params": params, "token": T.issue_token(coord, task_id="t-" + ttype, node_id=me.node_id, sid="S1", tools=[ttype])}, send)
            assert [(m[1]["state"], m[1]["reason"]) for m in sent] == [("rejected", "bad_parameters")], ttype
    run(go())


def test_the_allowlist_is_eight_read_only_types_and_the_two_named_write_types():
    assert set(node_tasks.ALLOWED) == {"node_report", "machine_analysis", "credential_presence", "list_dir", "model_list", *T.PROBES, *T.WRITE_TYPES} and T.WRITE_TYPES == ("setup_step", "environment_run", "credential_step")  # the credential change is the third write (contract 3.10), with its read-only presence probe
    assert set(T.PARAMS) == set(node_tasks.ALLOWED)
    read_only = set(node_tasks.ALLOWED) - set(T.WRITE_TYPES)
    for t in read_only:
        assert not {"plan", "step", "action", "writes", "scope", "approval"} & set(T.PARAMS[t].model_fields), t  # nothing a write would need can even be passed to a read-only task


# ---- the coordinator: it sends a write only when the log holds the owner's approval of that exact plan ---------------------------------------------------------

def _approve(c, plan, step=1, action="run", *, by=None):
    payload = {"plan_hash": plan["plan_hash"], "step": step, "action": action, **({"by": by} if by else {})}
    return c.log.add("setup_step_approved", payload, owner=True).id


def test_the_coordinator_sends_a_step_only_with_the_owners_approval_of_that_exact_plan_step_and_action(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            try:
                agent.runner.ctx.update({"recipes": {"demo": RECIPE}, "executors": {"fetch_check": write_file_executor}})
                plan = plan_dict()
                none = c.tasks.start_setup_step(nid, plan, 1)
                assert (none.status, none.reason) == ("rejected", "no_owner_approval")  # nobody approved: nothing was sent
                _approve(c, plan, 1, "run", by="claude-code")
                operator = c.tasks.start_setup_step(nid, plan, 1)
                assert operator.reason == "no_owner_approval"  # the operator's approval is never the owner's
                _approve(c, plan, 2, "run")
                other_step = c.tasks.start_setup_step(nid, plan, 1)
                assert other_step.reason == "no_owner_approval"
                altered = copy.deepcopy(plan)
                altered["steps"][0]["exact_command"] = "download something else"
                _approve(c, plan, 1, "run")
                assert c.tasks.start_setup_step(nid, altered, 1).reason == "plan_altered"
                assert c.tasks.start_setup_step(nid, {**plan, "plan_hash": "sha256:" + "0" * 64}, 1).reason == "plan_altered"
                rec = await finished(c.tasks.start_setup_step(nid, plan, 1))
                assert rec.status == "done" and rec.result["plan_hash"] == plan["plan_hash"]
                assert (agent.state.dir / "mas" / "downloads" / "m.gguf").read_text() == "model"
                assert [r.reason for r in c.tasks.list() if r.status == "rejected"].count("no_owner_approval") == 3
                sent = [e for e in events(c, "task_created") if e.payload.get("type") == "setup_step"]
                assert len(sent) == 6  # every attempt is recorded, the five that were refused and the one that ran
            finally:
                await down(agent, task)
    run(go())


def test_a_write_cannot_be_started_as_a_plain_task_and_the_token_names_the_approving_event(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            try:
                rec = c.tasks.start(nid, "setup_step", {"plan": plan_dict(), "step": 1}, by="OWNER")
                assert (rec.status, rec.reason) == ("rejected", "write_refused")  # the generic way to start a task never carries a write
                plan = plan_dict()
                approval_id = _approve(c, plan)
                seen = []
                real = c.tasks.sessions(nid).send

                async def spy(t, p):
                    seen.append((t, p))
                    await real(t, p)
                c.tasks.sessions(nid).send = spy
                agent.runner.ctx.update({"recipes": {"demo": RECIPE}, "executors": {"fetch_check": write_file_executor}})
                await finished(c.tasks.start_setup_step(nid, plan, 1))
                tok = seen[0][1]["token"]
                assert tok["scope"]["approval"] == {"actor": "OWNER", "event": approval_id, "plan_hash": plan["plan_hash"], "step": 1, "action": "run"} and tok["tools"] == ["setup_step"]
                assert tok["scope"]["writes"] == ["<mas>/downloads/m.gguf"] and tok["scope"]["recipe"] == "demo"
                read = c.tasks.start(nid, "os_details", {}, by="OWNER")
                await finished(read)
                assert "scope" not in T.issue_token(create(), task_id="x", node_id="n", sid="s", tools=["os_details"])  # a read-only task's token never carries a scope
            finally:
                await down(agent, task)
    run(go())
