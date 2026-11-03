"""environment_run (docs/NETWORK_CONTRACT.md 3.9 amended, docs/ROUTER_ENVIRONMENTS.md): the second task that writes, built like setup_step. SIMULATED-LOCAL: stand-in executables that mimic each CLI's
documented non-interactive interface, real TLS on loopback for the end-to-end tests, an httpx mock for the local model. Nothing here ran a real vendor tool."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from envrec_coordinator.router import DictFacts, EntryChat, MachineFacts, Policy, Refusal, Router, Task
from envrec_net import envrun as E
from envrec_net import task as T
from envrec_node import envrun as node_envrun
from mas_world import coordinator, events
from test_mas_tasks import agent_up, down, finished, make_runner

import envstandins as S

ENVS = E.load_environments()
ORIGIN = "evt_owner_origin"


def run(coro):
    return asyncio.run(coro)


def params_for(env, ttype="code", sens="public", ws="w1", endpoint=None, model=None, text="write hello"):
    entry, mode = ENVS[env], E.mode_of(ENVS[env], ttype)
    return {"environment_id": env, "task_type": ttype, "sensitivity": sens, "mode": mode["name"], "workspace_id": ws, "recipe_sha256": E.recipe_sha256(entry), "input_text": text, "endpoint": endpoint, "model_id": model}


def token_for(coord, me, params, *, task_id="t1", sid="S1", origin=ORIGIN, over=None, entry=None):
    entry = entry or ENVS[params["environment_id"]]
    sc = E.env_scope(entry=entry, task_type=params["task_type"], sensitivity=params["sensitivity"], workspace_id=params["workspace_id"], endpoint=params["endpoint"], origin_event=origin or "x", decision={"task_id": task_id})
    if origin is None:
        sc.pop("origin")
    sc.update(over or {})
    return T.issue_token(coord, task_id=task_id, node_id=me.node_id, sid=sid, tools=["environment_run"], scope=sc)


@pytest.fixture
def world(tmp_path):
    bins = S.install(tmp_path / "bin", tmp_path / "log")
    (tmp_path / "log").mkdir(exist_ok=True)
    S.install(tmp_path / "bin", tmp_path / "log")
    names = {"claude": bins["claude"], "codex": bins["codex"], "muse": bins["muse"], "opencode": bins["opencode"], "pi": bins["pi"]}
    r, coord, me = make_runner(tmp_path)
    (tmp_path / "mas").mkdir()
    r.ctx.update({"mas_home": tmp_path / "mas", "envs": ENVS, "os_family": "darwin", "find_exe": lambda n: names.get(n), "sandbox_backends": [], "environ": {"PATH": "/usr/bin", "HOME": "/home/x", "SECRET_TOKEN": "nope", "META_API_KEY": "k-123", "MODEL_API_KEY": "m-456"}})
    return r, coord, me, tmp_path


async def send_run(r, coord, me, params, *, task_id="t1", token="auto", **kw):
    sent = []

    async def send(t, p):
        sent.append((t, p))
    tok = token_for(coord, me, params, task_id=task_id, **kw) if token == "auto" else token
    await r.on_task({"task_id": task_id, "task_type": "environment_run", "params": params, "token": tok}, send)
    for _ in range(300):
        if any(m[0] == "result" or (m[0] == "task_update" and m[1]["state"] == "rejected") for m in sent):
            break
        await asyncio.sleep(0.02)
    return sent


def outcome(sent):
    last = sent[-1]
    return ("rejected", last[1]["reason"]) if last[0] == "task_update" else (last[1]["status"], last[1]["reason"] or last[1]["result"])


# ---- the node runs each environment with the documented arguments, in a workspace, and leaves nothing -----------------------------------------------------------------------------------

@pytest.mark.parametrize("env,ttype,needle", [("claude-code", "code", ["--tools", "Read,Write,Edit", "--disallowedTools", "Bash", "--permission-mode", "dontAsk"]), ("claude-code", "text", ["--tools", "", "--permission-mode", "dontAsk"]),
                                              ("opencode", "code", ["run", "--dir"]), ("opencode", "text", ["run"])])
def test_each_environment_runs_with_the_arguments_its_recipe_declares_and_the_workspace_is_removed(world, env, ttype, needle):
    r, coord, me, tmp = world
    kw = {"endpoint": "127.0.0.1:8080", "model": "local-8b"} if env == "opencode" else {}
    sent = asyncio.run(send_run(r, coord, me, params_for(env, ttype, **kw)))
    status, result = outcome(sent)
    assert status == "done", result
    name = {"claude-code": "claude", "codex": "codex", "opencode": "opencode"}[env]
    call = S.calls(tmp / "log", name)[0]
    flat = call["argv"]
    assert all(n in flat for n in needle), flat
    assert result["output_text"].strip() == f"answer from {name}" and result["exit_code"] == 0 and result["sandboxed"] is False
    assert result["files"] and any(f["name"] == "result.py" and f["content"] == "print('hi')\n" for f in result["files"])
    assert not (tmp / "mas" / "workspaces" / "w1").exists()  # removed after the task
    assert call["cwd"].endswith("workspaces/w1") or call["cwd"].endswith("workspaces/w1/".rstrip("/"))
    assert "SECRET_TOKEN" not in call["env"]  # only the variables the policy and the recipe name pass through


def test_the_input_travels_as_stdin_for_the_tools_that_read_it_and_as_a_task_prefixed_argument_for_the_others(world):
    r, coord, me, tmp = world
    asyncio.run(send_run(r, coord, me, params_for("claude-code", text="--evil -rf"), task_id="a"))
    assert S.calls(tmp / "log", "claude")[0]["stdin"] == "--evil -rf" and "--evil -rf" not in S.calls(tmp / "log", "claude")[0]["argv"]
    asyncio.run(send_run(r, coord, me, params_for("opencode", endpoint="127.0.0.1:8080", model="m", ws="w2", text="--evil"), task_id="b"))
    assert S.calls(tmp / "log", "opencode")[0]["argv"][-1] == "Task:\n--evil"  # a leading dash can never be read as a flag


def test_opencode_gets_the_loopback_endpoint_and_a_permission_set_with_bash_denied_and_never_ask(world):
    r, coord, me, tmp = world
    asyncio.run(send_run(r, coord, me, params_for("opencode", "code", endpoint="127.0.0.1:8080", model="local-8b")))
    env = S.calls(tmp / "log", "opencode")[0]["env"]
    perm = json.loads(env["OPENCODE_PERMISSION"])
    assert perm == {"bash": "deny", "webfetch": "deny", "external_directory": "deny", "edit": "allow"} and "ask" not in env["OPENCODE_PERMISSION"]
    cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert cfg["provider"]["llamacpp"]["options"]["baseURL"] == "http://127.0.0.1:8080/v1" and "local-8b" in cfg["provider"]["llamacpp"]["models"]


def test_an_environment_that_needs_a_sandbox_is_wrapped_by_the_backend_and_its_key_travels_by_name_only(world):
    r, coord, me, tmp = world
    r.ctx["sandbox_backends"] = ["docker"]
    seen = {}

    def fake_run(argv, cwd, env, *, timeout_s, stdin_text=None):
        seen.update(argv=argv, env=env, stdin=stdin_text)
        return {"exit": 0, "stdout": "answer", "stderr": "", "timed_out": False, "duration_s": 0.1}
    r.ctx["run"] = fake_run
    sent = asyncio.run(send_run(r, coord, me, params_for("muse-code", "text"), ))
    assert outcome(sent)[0] == "done"
    argv = seen["argv"]
    assert argv[:2] == ["docker", "run"] and "-e" in argv and "META_API_KEY" in argv and "MODEL_API_KEY" in argv and "k-123" not in " ".join(argv) and "m-456" not in " ".join(argv)
    assert seen["env"]["META_API_KEY"] == "k-123" and seen["env"]["MODEL_API_KEY"] == "m-456" and "SECRET_TOKEN" not in seen["env"] and outcome(sent)[1]["sandboxed"] is True
    assert argv[-3:-1] == ["muse", "exec"] and argv[-1].startswith("Task:\n")


def test_codex_runs_only_in_the_sandbox_with_the_workspace_where_the_backend_mounts_it_for_both_task_types(world):
    r, coord, me, tmp = world
    seen = []

    def fake_run(argv, cwd, env, *, timeout_s, stdin_text=None):
        seen.append((argv, stdin_text))
        (cwd / ".last-message.txt").write_text("done")
        return {"exit": 0, "stdout": "", "stderr": "", "timed_out": False, "duration_s": 0.1}
    r.ctx["run"] = fake_run
    assert outcome(asyncio.run(send_run(r, coord, me, params_for("codex", "text"), task_id="a"))) == ("rejected", "sandbox_missing")  # no backend: refused for text too
    r.ctx["sandbox_backends"] = ["docker"]
    for ttype, sandbox_flag, tid in (("text", "read-only", "b"), ("code", "workspace-write", "c")):
        sent = asyncio.run(send_run(r, coord, me, params_for("codex", ttype, ws="w-" + tid), task_id=tid))
        assert outcome(sent)[0] == "done" and outcome(sent)[1]["output_text"] == "done"
        argv = seen[-1][0]
        assert argv[:2] == ["docker", "run"] and argv[argv.index("-s") + 1] == sandbox_flag and argv[argv.index("-C") + 1] == "/work" and argv[argv.index("-o") + 1] == "/work/.last-message.txt" and argv[-1] == "-"
        assert seen[-1][1] == "write hello"


def test_pi_gets_its_home_redirected_into_the_workspace_and_a_models_json_for_the_loopback_server(world):
    r, coord, me, tmp = world
    r.ctx["sandbox_backends"] = ["docker"]
    written = {}

    def fake_run(argv, cwd, env, *, timeout_s, stdin_text=None):
        written["models"] = json.loads((cwd / ".home" / ".pi" / "agent" / "models.json").read_text())
        written["env"] = env
        return {"exit": 0, "stdout": "ok", "stderr": "", "timed_out": False, "duration_s": 0.1}
    r.ctx["run"] = fake_run
    sent = asyncio.run(send_run(r, coord, me, params_for("pi", "code", endpoint="127.0.0.1:8080", model="local-8b")))
    assert outcome(sent)[0] == "done"
    assert written["models"]["providers"]["llama-cpp"]["baseUrl"] == "http://127.0.0.1:8080/v1" and written["env"]["HOME"] == "/work/.home" and written["env"]["PI_OFFLINE"] == "1"
    assert not (tmp / "mas" / "workspaces" / "w1").exists()


def test_the_local_model_environment_posts_the_input_to_the_loopback_server(world):
    r, coord, me, tmp = world
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": " Summary. "}}]})
    r.ctx["transport"] = httpx.MockTransport(handler)
    sent = asyncio.run(send_run(r, coord, me, params_for("llamacpp-local", "text", sens="confidential", endpoint="127.0.0.1:8080", model="local-8b", text="Summarize this.")))
    status, result = outcome(sent)
    assert status == "done" and result["output_text"] == "Summary." and seen["url"] == "http://127.0.0.1:8080/v1/chat/completions" and seen["body"]["messages"][0]["content"] == "Summarize this."


# ---- the node refuses, with a typed reason, before it accepts anything ---------------------------------------------------------------------------------------------------------------

def _refuse(world, params, *, entry=None, **kw):
    r, coord, me, tmp = world
    sent = asyncio.run(send_run(r, coord, me, params, **kw))
    return outcome(sent), tmp


@pytest.mark.parametrize("name", ["unknown_environment", "stale_recipe", "unsupported_os", "no_owner_origin", "bad_scope", "confidential_cloud", "not_loopback", "sandbox_missing", "not_installed", "bad_parameters"])
def test_an_environment_run_is_refused_with_its_typed_reason_and_nothing_is_started(world, name):
    r, coord, me, tmp = world
    params = params_for("opencode", "code", endpoint="127.0.0.1:8080", model="local-8b")
    kw = {}
    if name == "unknown_environment":
        r.ctx["envs"] = {k: v for k, v in ENVS.items() if k != "opencode"}
    elif name == "stale_recipe":
        r.ctx["envs"] = {**ENVS, "opencode": {**ENVS["opencode"], "adapter": {**ENVS["opencode"]["adapter"], "timeout_s": 1}}}
    elif name == "unsupported_os":
        r.ctx["os_family"] = "plan9"
    elif name == "no_owner_origin":
        kw["origin"] = None
    elif name == "bad_scope":
        kw["over"] = {"workspace_id": "other"}
    elif name == "confidential_cloud":
        params = params_for("claude-code", "text", sens="confidential")
    elif name == "not_loopback":
        params = params_for("opencode", "code", endpoint="localhost:8080", model="local-8b")
    elif name == "sandbox_missing":
        params = params_for("pi", "code", endpoint="127.0.0.1:8080", model="m")
    elif name == "not_installed":
        r.ctx["find_exe"] = lambda n: None
    elif name == "bad_parameters":
        params = {**params, "command": "rm -rf /"}
    sent = asyncio.run(send_run(r, coord, me, params, **kw))
    assert outcome(sent) == ("rejected", name), name
    assert [m[1]["state"] for m in sent if m[0] == "task_update"] == ["rejected"] and not (tmp / "mas" / "workspaces").exists()


def test_a_lan_address_a_wildcard_and_a_name_are_never_a_model_endpoint():
    for bad in ("localhost:8080", "0.0.0.0:8080", "192.168.1.5:8080", "127.0.0.2:8080", "127.0.0.1", "[::1]:99999", "evil.test:80"):
        with pytest.raises(ValueError):
            E.parse_endpoint(bad)
    assert E.parse_endpoint("127.0.0.1:8080") == ("127.0.0.1", 8080) and E.parse_endpoint("[::1]:8080") == ("::1", 8080)


def test_a_token_is_single_use_and_a_second_run_in_a_taken_workspace_fails(world):
    r, coord, me, tmp = world
    params = params_for("claude-code")
    tok = token_for(coord, me, params)
    first = asyncio.run(send_run(r, coord, me, params, token=tok))
    again = asyncio.run(send_run(r, coord, me, params, token=tok))
    assert outcome(first)[0] == "done" and outcome(again) == ("rejected", "token_reused")


# ---- writes outside the workspace -----------------------------------------------------------------------------------------------------------------------------------------------

def test_an_environment_that_writes_outside_its_workspace_is_stopped_what_it_wrote_is_removed_and_the_task_fails(world):
    r, coord, me, tmp = world
    S.set_mode(tmp / "log", "claude", "escape")
    sent = asyncio.run(send_run(r, coord, me, params_for("claude-code")))
    assert outcome(sent)[0] == "failed" and sent[-1][1]["reason"] == "outside_workspace"
    assert not (tmp / "mas" / "workspaces" / "escape.txt").exists() and not (tmp / "mas" / "workspaces" / "w1").exists()


def test_a_link_out_of_the_workspace_is_refused_and_a_failing_or_silent_environment_fails_with_its_reason(world):
    r, coord, me, tmp = world
    S.set_mode(tmp / "log", "claude", "symlink")
    assert sent_reason(asyncio.run(send_run(r, coord, me, params_for("claude-code")))) == "outside_workspace"
    S.set_mode(tmp / "log", "claude", "fail")
    assert sent_reason(asyncio.run(send_run(r, coord, me, params_for("claude-code", ws="w2"), task_id="t2"))) == "environment_failed"


def sent_reason(sent):
    return sent[-1][1]["reason"]


# ---- the whole path: route -> dispatch -> signed task -> node -> result (real TLS on loopback) ---------------------------------------------------------------------------------------------------

def build(c, nid, *, machine=None, adm=None, pol=None, payload="Write hello.", **facts):
    m = machine or MachineFacts(machine_id=nid, os_family="darwin", installed=set(), signed_in=set(), env_names=set(), sandbox_backends=[], model_endpoint=None, model_id=None)
    p = Policy.defaults()
    p.open_subtypes = {"local_model", "own_harness", "provider"}
    p.automated_use_cleared = {"claude-code"}
    p.approved_providers_for_internal = {"claude-code"}
    for k, v in (pol or {}).items():
        setattr(p, k, v)
    log = []
    r = Router(envs=ENVS, facts=DictFacts([m], admission=dict(adm or []), **facts), policy=p, log=lambda t, a, actor: (log.append((t, actor, a)), c.log.add(t, a)), sender=c.tasks, payloads=lambda ref: payload)
    return r, log, m


def task(tid, ttype="code", sens="public", chat="cloud"):
    return Task(task_id=tid, task_type=ttype, sensitivity=sens, entry_chat=EntryChat(kind=chat, name="claude-desktop" if chat == "cloud" else "owner-panel"), created_by="OWNER", payload_ref="ref")


def node_up(agent, tmp, **ctx):
    bins = S.install(tmp / "bin", tmp / "log")
    names = dict(bins)
    agent.runner.ctx.update({"envs": ENVS, "os_family": "darwin", "find_exe": lambda n: names.get(n), "sandbox_backends": [], "environ": {"PATH": "/usr/bin", "HOME": "/home/x"}, **ctx})


def test_a_public_code_task_from_a_cloud_chat_runs_in_a_provider_stand_in_and_the_token_names_the_owners_event(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                (tmp_path / "log").mkdir(exist_ok=True)
                node_up(agent, tmp_path)
                m = MachineFacts(machine_id=nid, os_family="darwin", installed={"claude-code"}, signed_in={"claude-code:plan"})
                r, log, _ = build(c, nid, machine=m)
                d = r.route(task("pub1"))
                assert d.route.environment_id == "claude-code" and d.evidence_label == "no data"
                origin = c.own_task("pub1")
                seen = []
                real = c.tasks.sessions(nid).send

                async def spy(t, p):
                    seen.append((t, p))
                    await real(t, p)
                c.tasks.sessions(nid).send = spy
                h = r.dispatch(d)
                rec = await finished(c.tasks.tasks[h.node_task_id])
                assert rec.status == "done" and rec.result["output_text"].strip() == "answer from claude" and rec.result["confinement"] == "tool_declared"
                sc = seen[0][1]["token"]["scope"]
                assert sc["origin"] == {"actor": "OWNER", "event": origin} and sc["environment"] == "claude-code" and sc["recipe_sha256"] == E.recipe_sha256(ENVS["claude-code"]) and seen[0][1]["token"]["tools"] == ["environment_run"]
                assert ("route_decision", "ROUTER") in [(t, a) for t, a, _ in log] and ("dispatch", "GUARD") in [(t, a) for t, a, _ in log]
            finally:
                await down(agent, tsk)
    run(go())


def test_a_confidential_text_task_from_a_local_chat_runs_on_the_local_model_and_the_same_task_from_a_cloud_chat_is_refused_without_sending_anything(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                (tmp_path / "log").mkdir(exist_ok=True)
                node_up(agent, tmp_path, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "the summary"}}]})))
                m = MachineFacts(machine_id=nid, os_family="darwin", installed=set(), signed_in=set(), model_endpoint="127.0.0.1:8080", model_id="local-8b")
                key = f"admission.text.{nid}.llamacpp-local.local-8b.local-hardware"
                r, log, _ = build(c, nid, machine=m, adm=[(key, {"status": "tested", "graded_by": "code", "passed": 9, "of": 10, "identity": {}})])
                before = len([e for e in events(c, "task_created")])
                refused = r.route(task("conf-cloud", "text", "confidential", "cloud"))
                assert isinstance(refused, Refusal) and refused.code == "confidential_from_cloud_chat"
                assert len(events(c, "task_created")) == before  # nothing was created, nothing was sent
                d = r.route(task("conf-local", "text", "confidential", "local"))
                assert d.route.environment_id == "llamacpp-local" and d.evidence_label == "measured here 9/10"
                c.own_task("conf-local")
                rec = await finished(c.tasks.tasks[r.dispatch(d).node_task_id])
                assert rec.status == "done" and rec.result["output_text"] == "the summary"
            finally:
                await down(agent, tsk)
    run(go())


def test_without_an_owner_event_for_the_task_nothing_leaves_and_the_operators_event_does_not_count(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                (tmp_path / "log").mkdir(exist_ok=True)
                node_up(agent, tmp_path)
                m = MachineFacts(machine_id=nid, os_family="darwin", installed={"claude-code"}, signed_in={"claude-code:plan"})
                r, _, _ = build(c, nid, machine=m)
                d = r.route(task("p"))
                none = r.dispatch(d)
                assert isinstance(none, Refusal) and none.code == "no_owner_origin"
                c.own_task("p", by="claude-code")
                r2, _, _ = build(c, nid, machine=m)
                assert r2.dispatch(r2.route(task("p"))).code == "no_owner_origin"  # the operator is counted apart, never as the owner
                assert [x.reason for x in c.tasks.list() if x.task_type == "environment_run"] == ["no_owner_origin", "no_owner_origin"]
                generic = c.tasks.start(nid, "environment_run", params_for("claude-code"), by="OWNER")
                assert (generic.status, generic.reason) == ("rejected", "write_refused")  # the generic way to start a task never carries this write either
            finally:
                await down(agent, tsk)
    run(go())


def test_a_failing_grader_escalates_through_three_environments_and_then_stops_with_a_notice(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                (tmp_path / "log").mkdir(exist_ok=True)
                node_up(agent, tmp_path, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "wrong"}}]})))
                m = MachineFacts(machine_id=nid, os_family="darwin", installed={"opencode", "claude-code"}, signed_in={"claude-code:plan"}, model_endpoint="127.0.0.1:8080", model_id="local-8b")
                r, log, _ = build(c, nid, machine=m)
                c.own_task("esc")
                used = []
                d = r.route(task("esc"))
                for step in range(3):
                    used.append(d.route.environment_id)
                    rec = await finished(c.tasks.tasks[r.dispatch(d).node_task_id])
                    assert rec.status == "done"  # the environment ran; the code grader is what fails
                    d = r.report_result("esc", step, {"passed": False, "grader": "code", "evidence_ref": "g"})
                assert used == ["llamacpp-local", "opencode", "claude-code"]
                assert isinstance(d, Refusal) and d.code == "escalation_exhausted" and d.details["owner_notice"]["text_fallback"].startswith("Task esc could not be completed")
                assert [t for t, a, _ in log].count("escalation") == 2
            finally:
                await down(agent, tsk)
    run(go())


def test_a_coding_environment_that_writes_outside_its_workspace_fails_the_dispatched_task(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                (tmp_path / "log").mkdir(exist_ok=True)
                node_up(agent, tmp_path)
                S.set_mode(tmp_path / "log", "claude", "escape")
                m = MachineFacts(machine_id=nid, os_family="darwin", installed={"claude-code"}, signed_in={"claude-code:plan"})
                r, _, _ = build(c, nid, machine=m)
                c.own_task("w")
                rec = await finished(c.tasks.tasks[r.dispatch(r.route(task("w"))).node_task_id])
                assert (rec.status, rec.reason) == ("failed", "outside_workspace") and not (agent.state.dir / "mas" / "workspaces" / "escape.txt").exists()
            finally:
                await down(agent, tsk)
    run(go())
