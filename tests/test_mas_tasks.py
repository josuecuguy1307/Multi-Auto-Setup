"""Signed read-only tasks from the coordinator to a node (protocol-task) and the first two: node_report and machine_analysis. SIMULATED-LOCAL: real TLS on loopback; the node is a real agent in this process."""
from __future__ import annotations

import asyncio
import time

import pytest

import envrec_os
from envrec_coordinator.tasks import evaluate_report
from envrec_net import task as T
from envrec_net.identity import create
from envrec_node import tasks as node_tasks
from envrec_node.agent import NodeAgent
from mas_world import Clock, coordinator, events, offer, pair_node, wait_event

CODE = "482915"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def quiet_node(monkeypatch):
    """The node under test shares a process with the coordinator, which listens: report what a real node would (nothing), unless a test says otherwise. Analysis is replaced by a quick fake."""
    monkeypatch.setattr(envrec_os, "own_listening_sockets", lambda: [])
    monkeypatch.setattr(node_tasks.analysis, "run_analysis", lambda params: ({"system": {"os.name": "x"}, "hardware": {}, "programs": {}, "ai": {}, "limits": []}, [{"provider": "fake"}]))


async def agent_up(c, tmp, name="node1"):
    offer(c, CODE, oid=f"o-{name}")
    await pair_node(c, tmp, CODE, node=name)
    agent = NodeAgent(tmp / name, interval_s=0.2, clock=c.clock)
    task = asyncio.create_task(agent.run())
    nid = c.registry.all()[-1].node_id
    for _ in range(300):
        if nid in c._sessions:
            break
        await asyncio.sleep(0.02)
    return agent, task, nid


async def down(agent, task):
    agent.stop_event.set()
    await asyncio.wait_for(task, 5)


async def finished(rec, timeout=10):
    await asyncio.wait_for(rec.done.wait(), timeout)
    return rec


def states_of(c, rec):
    return [(e.payload["from"], e.payload["to"]) for e in events(c, "protocol_state") if e.payload.get("subject") == rec.id]


# ---- the self-report ------------------------------------------------------------------------------------------------------------------------

def test_a_node_sends_its_self_report_by_itself_when_it_connects_and_the_coordinator_stores_it_with_no_alerts(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            for _ in range(200):
                if c.registry.get(nid).report:
                    break
                await asyncio.sleep(0.02)
            rep = c.registry.get(nid).report
            assert rep["os"]["family"] == envrec_os.family() and rep["sockets"] == [] and rep["key"]["owner_only"] is True and rep["key"]["path"].endswith("key.pem")
            assert c.registry.get(nid).alerts == [] and c.registry.get(nid).report_at == c.clock()
            rec = c.tasks.list()[0]
            assert rec.task_type == "node_report" and rec.by == "GUARD" and rec.status == "done"
            assert states_of(c, rec) == [("created", "sent"), ("sent", "accepted"), ("accepted", "running"), ("running", "done")]
            await down(agent, task)
    run(go())


def test_a_node_with_a_listening_port_or_a_world_readable_key_raises_alerts_logged_as_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(envrec_os, "own_listening_sockets", lambda: [{"proto": "tcp", "ip": "0.0.0.0", "port": 9999}])
    monkeypatch.setattr(envrec_os, "key_file_status", lambda p: {"path": str(p), "exists": True, "owner_only": False})

    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            for _ in range(200):
                if c.registry.get(nid).alerts:
                    break
                await asyncio.sleep(0.02)
            kinds = sorted(a["kind"] for a in c.registry.get(nid).alerts)
            assert kinds == ["key_not_private", "listening_ports"]
            alerts = events(c, "node_alert")
            assert sorted(e.payload["kind"] for e in alerts) == kinds and all(e.effective_tag == "GUARD" for e in alerts)
            row = [m for m in c.snapshot()["machines"] if m["id"] == nid][0]
            assert {a["kind"] for a in row["alerts"]} == set(kinds)
            await down(agent, task)
    run(go())


def test_evaluate_report_alerts_on_ports_key_and_large_skew_and_is_silent_otherwise():
    ok = {"sockets": [], "key": {"exists": True, "owner_only": True}}
    assert evaluate_report(ok, 10) == []
    assert [a["kind"] for a in evaluate_report({**ok, "sockets": [{"proto": "udp", "ip": "0.0.0.0", "port": 7444}]})] == ["listening_ports"]
    assert [a["kind"] for a in evaluate_report({**ok, "key": {"exists": True, "owner_only": False}})] == ["key_not_private"]
    assert evaluate_report({**ok, "key": {"exists": False, "owner_only": None}}) == []
    sk = evaluate_report(ok, -420)
    assert [a["kind"] for a in sk] == ["clock_skew"] and sk[0]["skew_s"] == -420 and "7 min" in sk[0]["text"]
    assert evaluate_report(ok, 299) == []


def test_the_per_os_self_report_functions_work_on_this_machine(tmp_path):
    info = envrec_os.os_info()
    assert info["family"] in ("darwin", "linux", "windows") and info["arch"] and info["release"]
    key = tmp_path / "k.pem"
    key.write_text("x")
    envrec_os.current().protect_private(key)
    assert envrec_os.key_file_status(key) == {"path": str(key), "exists": True, "owner_only": True}
    assert envrec_os.key_file_status(tmp_path / "missing.pem") == {"path": str(tmp_path / "missing.pem"), "exists": False, "owner_only": None}


def test_own_listening_sockets_sees_this_processs_own_listener_so_the_alert_path_is_not_blind(monkeypatch):
    import socket
    monkeypatch.undo()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        assert {"proto": "tcp", "ip": "127.0.0.1", "port": s.getsockname()[1]} in envrec_os.own_listening_sockets()
    finally:
        s.close()


# ---- the owner asks for an analysis ------------------------------------------------------------------------------------------------------------

def test_an_owner_requested_analysis_runs_on_the_node_and_comes_back_signed_typed_and_stored(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            view = c.run_task(nid, "machine_analysis", "web")
            rec = await finished(c.tasks.tasks[view["id"]])
            assert rec.status == "done" and rec.by == "OWNER" and rec.result["system"] == {"os.name": "x"} and rec.evidence == [{"provider": "fake"}]
            assert c.registry.load_analysis(nid)["system"] == {"os.name": "x"} and c.registry.get(nid).analysis_at == c.clock()
            created = [e for e in events(c, "task_created") if e.payload["task"] == rec.id]
            assert created[0].effective_tag == "OWNER"
            assert states_of(c, rec) == [("created", "sent"), ("sent", "accepted"), ("accepted", "running"), ("running", "done")]
            assert all(e.effective_tag in ("GUARD", "OWNER") for e in events(c))
            await down(agent, task)
    run(go())


def test_the_real_machine_analysis_on_this_machine_returns_grouped_facts_and_never_a_secret_value(tmp_path, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-appear-in-any-result-1234567890")
    from envrec_node import analysis
    result, evidence = analysis.run_analysis(T.MachineAnalysisParams(max_apps=5))
    assert set(result) >= {"system", "hardware", "programs", "ai", "limits"} and result["system"]["os.name"] and result["hardware"]["hw.ram_total_gib"] > 0
    assert {"provider": "openai", "name": "OPENAI_API_KEY"} in result["ai"]["api_key_variable_names"]
    assert "sk-should-never-appear" not in repr(result) + repr(evidence)
    assert all("port" not in app for app in result["programs"]["installed_apps"])  # a model server is an AI fact, not an installed app
    assert result["ai"]["largest_model_that_fits"]["limited_by"] in ("ram", "disk", "config_params", "none") and evidence
    assert any(e["provider"] in ("platform", "psutil", "stdlib") for e in evidence)


# ---- what the node refuses ----------------------------------------------------------------------------------------------------------------------

def make_runner(tmp_path, sid="S1"):
    coord, me = create(), create()
    r = node_tasks.TaskRunner(key_path=tmp_path / "k.pem")
    r.new_session(T.TokenChecker(coord.public_raw, me.node_id, sid))
    return r, coord, me


async def offer_task(r, coord, me, task_type="node_report", params=None, *, sid="S1", task_id="t1", token=None, tools=None):
    sent = []

    async def send(t, p):
        sent.append((t, p))
    tok = token if token is not None else T.issue_token(coord, task_id=task_id, node_id=me.node_id, sid=sid, tools=tools or [task_type])
    await r.on_task({"task_id": task_id, "task_type": task_type, "params": params or {}, "token": tok}, send)
    await asyncio.sleep(0.05)
    return sent


@pytest.mark.parametrize("task_type", ["write_file", "shell", "exec", "install_package", "../../etc/passwd", "", None, 7])
def test_a_task_that_is_not_on_the_read_only_allowlist_is_rejected_whatever_it_is_called(tmp_path, task_type):
    async def go():
        r, coord, me = make_runner(tmp_path)
        sent = []

        async def send(t, p):
            sent.append((t, p))
        tok = T.issue_token(coord, task_id="t1", node_id=me.node_id, sid="S1", tools=["node_report", str(task_type)])  # even a token that names the tool does not make it allowed
        await r.on_task({"task_id": "t1", "task_type": task_type, "params": {}, "token": tok}, send)
        assert [(m[1]["state"], m[1]["reason"]) for m in sent] == [("rejected", "tool_not_allowed")] and r.running == 0
    run(go())


def test_a_write_or_a_shell_command_smuggled_in_a_parameter_is_refused_as_bad_parameters(tmp_path):
    async def go():
        for params in ({"command": "rm -rf /"}, {"path": "/etc/hosts", "content": "x"}, {"shell": True}, {"include_apps": "yes please", "max_apps": 10 ** 6}):
            r, coord, me = make_runner(tmp_path)
            ttype = "machine_analysis" if "include_apps" in params else "node_report"
            sent = await offer_task(r, coord, me, ttype, params)
            assert [(m[1]["state"], m[1]["reason"]) for m in sent] == [("rejected", "bad_parameters")], params
            assert r.running == 0
    run(go())


def test_the_not_allowed_tool_is_refused_before_anything_runs(tmp_path):
    async def go():
        r, coord, me = make_runner(tmp_path)
        sent = await offer_task(r, coord, me, "write_file", {"path": "x"}, tools=["write_file"])
        assert [(m[0], m[1]["state"], m[1]["reason"]) for m in sent] == [("task_update", "rejected", "tool_not_allowed")] and r.running == 0
        assert set(node_tasks.ALLOWED) == {"node_report", "machine_analysis"}  # the allowlist is exactly these two
    run(go())


@pytest.mark.parametrize("case,reason", [("forged", "bad_token"), ("tampered", "bad_token"), ("other_node", "token_wrong_node"), ("other_session", "token_wrong_session"),
                                         ("wrong_task", "token_tool_mismatch"), ("wrong_tool", "token_tool_mismatch"), ("missing", "bad_token"), ("garbage", "bad_token")])
def test_a_bad_capability_token_is_rejected_with_its_typed_reason_and_nothing_runs(tmp_path, case, reason):
    async def go():
        r, coord, me = make_runner(tmp_path)
        other = create()
        tok = {"forged": T.issue_token(create(), task_id="t1", node_id=me.node_id, sid="S1", tools=["node_report"]),
               "tampered": {**T.issue_token(coord, task_id="t1", node_id=me.node_id, sid="S1", tools=["node_report"]), "tools": ["node_report", "machine_analysis"]},
               "other_node": T.issue_token(coord, task_id="t1", node_id=other.node_id, sid="S1", tools=["node_report"]),
               "other_session": T.issue_token(coord, task_id="t1", node_id=me.node_id, sid="S2", tools=["node_report"]),
               "wrong_task": T.issue_token(coord, task_id="t9", node_id=me.node_id, sid="S1", tools=["node_report"]),
               "wrong_tool": T.issue_token(coord, task_id="t1", node_id=me.node_id, sid="S1", tools=["machine_analysis"]),
               "missing": None, "garbage": "not a token"}[case]
        sent = []

        async def send(t, p):
            sent.append((t, p))
        await r.on_task({"task_id": "t1", "task_type": "node_report", "params": {}, "token": tok}, send)
        assert [(m[1]["state"], m[1]["reason"]) for m in sent] == [("rejected", reason)] and r.running == 0
    run(go())


def test_a_token_is_single_use_and_expires_by_the_nodes_own_monotonic_clock(tmp_path):
    async def go():
        r, coord, me = make_runner(tmp_path)
        tok = T.issue_token(coord, task_id="t1", node_id=me.node_id, sid="S1", tools=["node_report"])
        first = await offer_task(r, coord, me, token=tok)
        assert [m[1].get("state") for m in first][:2] == ["accepted", "running"] and first[-1][0] == "result" and first[-1][1]["status"] == "done"
        again = await offer_task(r, coord, me, token=tok)
        assert [(m[1]["state"], m[1]["reason"]) for m in again] == [("rejected", "token_reused")]
        # expiry: a task that waited longer than its ttl before it could start
        now = [100.0]
        r2 = node_tasks.TaskRunner(key_path=tmp_path / "k.pem", mono=lambda: now[0])
        r2.new_session(T.TokenChecker(coord.public_raw, me.node_id, "S1"))
        short = T.issue_token(coord, task_id="t2", node_id=me.node_id, sid="S1", tools=["node_report"], ttl_s=5)
        sent = []

        async def send(t, p):
            sent.append((t, p))
            now[0] += 60  # the node was busy for a minute before it could start the task
        await r2.on_task({"task_id": "t2", "task_type": "node_report", "params": {}, "token": short}, send)
        await asyncio.sleep(0.1)
        assert sent[-1][1]["status"] == "failed" and sent[-1][1]["reason"] == "token_expired"
    run(go())


def test_a_node_runs_at_most_its_configured_number_of_tasks_at_once(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setitem(node_tasks.ALLOWED, "node_report", lambda p, ctx: (time.sleep(0.4) or {"os": {}}, []))
        r, coord, me = make_runner(tmp_path)
        r.max_concurrent = 2
        sent = []

        async def send(t, p):
            sent.append((t, p))
        for i in range(3):
            await r.on_task({"task_id": f"t{i}", "task_type": "node_report", "params": {}, "token": T.issue_token(coord, task_id=f"t{i}", node_id=me.node_id, sid="S1", tools=["node_report"])}, send)
        assert [m[1]["reason"] for m in sent if m[1].get("state") == "rejected"] == ["busy"]
        await asyncio.sleep(0.7)
    run(go())


def test_the_node_never_imports_a_shell_or_dispatches_by_name():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(node_tasks))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not ({"subprocess", "os", "runpy", "importlib", "shlex", "pty"} & mods), mods
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not ({"eval", "exec", "compile", "getattr", "__import__", "globals"} & called), called


# ---- the coordinator's side: offline, timeout, cancel, concurrency, disconnect, forged results --------------------------------------------------

def test_a_task_for_a_node_that_is_not_connected_fails_at_once_and_is_not_queued(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            rec = c.tasks.start("n-nobody", "node_report", by="OWNER")
            assert rec.status == "failed" and rec.reason == "node_offline" and rec.done.is_set()
            assert [e.payload["reason"] for e in events(c, "task_finished")] == ["node_offline"] and "not connected" in T.REJECTIONS["node_offline"]
            assert states_of(c, rec) == [("created", "failed")]
    run(go())


def test_an_unknown_or_malformed_task_is_rejected_by_the_coordinator_before_sending(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            for ttype, params in (("write_file", {}), ("node_report", {"command": "id"}), ("machine_analysis", {"max_apps": -1})):
                rec = c.tasks.start(nid, ttype, params, by="OWNER")
                assert rec.status == "rejected" and rec.reason in ("tool_not_allowed", "bad_parameters") and states_of(c, rec) == [("created", "rejected")]
            await down(agent, task)
    run(go())


def test_a_task_that_outlives_its_timeout_ends_as_timed_out_the_node_is_told_to_cancel_and_a_late_result_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setitem(node_tasks.ALLOWED, "machine_analysis", lambda p, ctx: (time.sleep(1.0) or {"late": True}, []))

    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            rec = c.tasks.start(nid, "machine_analysis", None, by="OWNER", timeout_s=0.3)
            await finished(rec)
            assert rec.status == "timed_out" and states_of(c, rec)[-1][1] == "timed_out"
            await asyncio.sleep(1.3)  # the node's worker finishes later; its result must change nothing
            assert rec.status == "timed_out" and rec.result == {} and c.registry.load_analysis(nid) is None
            await down(agent, task)
    run(go())


def test_the_owner_can_cancel_a_running_task_and_its_result_never_arrives(tmp_path, monkeypatch):
    monkeypatch.setitem(node_tasks.ALLOWED, "machine_analysis", lambda p, ctx: (time.sleep(1.0) or {"late": True}, []))

    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            rec = c.tasks.start(nid, "machine_analysis", None, by="OWNER")
            for _ in range(100):
                if rec.status == "running":
                    break
                await asyncio.sleep(0.02)
            assert await c.tasks.cancel(rec.id) is True
            assert rec.status == "cancelled" and states_of(c, rec)[-1] == ("running", "cancelled")
            assert await c.tasks.cancel(rec.id) is False  # already over
            await asyncio.sleep(1.3)
            assert rec.status == "cancelled" and c.registry.load_analysis(nid) is None
            await down(agent, task)
    run(go())


def test_at_most_n_tasks_run_on_one_node_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setitem(node_tasks.ALLOWED, "machine_analysis", lambda p, ctx: (time.sleep(0.6) or {}, []))

    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            await asyncio.sleep(0.2)
            recs = [c.tasks.start(nid, "machine_analysis", None, by="OWNER") for _ in range(4)]
            assert [r.status for r in recs].count("rejected") >= 2 and {r.reason for r in recs if r.status == "rejected"} == {"busy"}
            await asyncio.sleep(1.0)
            await down(agent, task)
    run(go())


def test_a_node_that_disconnects_mid_task_fails_the_task_with_a_clear_reason(tmp_path, monkeypatch):
    monkeypatch.setitem(node_tasks.ALLOWED, "machine_analysis", lambda p, ctx: (time.sleep(1.0) or {}, []))

    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            rec = c.tasks.start(nid, "machine_analysis", None, by="OWNER")
            await asyncio.sleep(0.3)
            await down(agent, task)
            await finished(rec, 5)
            assert rec.status == "failed" and rec.reason == "node_disconnected"
    run(go())


def test_a_result_for_a_task_the_coordinator_did_not_make_or_from_another_node_changes_nothing(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            c.tasks.on_node_result(nid, {"task_id": "never-made", "status": "done", "result": {"x": 1}})
            rec = c.tasks.start(nid, "machine_analysis", None, by="OWNER")
            c.tasks.on_node_result("n-someone-else", {"task_id": rec.id, "status": "done", "result": {"forged": True}})
            assert rec.status != "done" or rec.result != {"forged": True}
            ignored = events(c, "task_message_ignored")
            assert len(ignored) >= 2
            await finished(rec)
            assert rec.result.get("system") == {"os.name": "x"}  # the real result, not the forged one
            await down(agent, task)
    run(go())


def test_the_protocol_refuses_an_illegal_move_and_logs_it(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            rec = c.tasks.start("n-nobody", "node_report", by="OWNER")  # ended as failed
            from envrec_net.protocol import IllegalTransition
            rec.guard.record("task.running", True)
            with pytest.raises(IllegalTransition):
                rec.guard.advance("running")
            assert events(c, "protocol_refused")
    run(go())


def test_a_result_with_the_wrong_shape_fails_the_task_instead_of_being_believed(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await agent_up(c, tmp_path)
            await asyncio.sleep(0.3)
            rec = c.tasks.start(nid, "machine_analysis", None, by="OWNER")
            c.tasks.on_node_result(nid, {"task_id": rec.id, "status": "done", "result": "not a dict", "extra": 1})
            assert rec.status == "failed" and "agreed form" in rec.reason
            await down(agent, task)
    run(go())
