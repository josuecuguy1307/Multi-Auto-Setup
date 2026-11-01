"""The world of the router acceptance examples (docs/ROUTER_ACCEPTANCE.md section 0.1), SIMULATED-LOCAL: a real coordinator (the Mac), a real node over loopback TLS (the Lenovo), the real MCP server
as a subprocess, the owner's system dialogs played by a test, the in-memory credential store, and scripted model answers behind the node's HTTP calls and its OpenCode runner. Everything the
router, the guards, the admission desk and the tools do is real; only the models and the owner's hands are played. Nothing here decides a route."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import keyring
from keyring.backend import KeyringBackend

from envrec_coordinator.admission.suite import load_suite
from envrec_net import envrun as E
from envrec_node import credentials as C
from mas_world import coordinator
from test_harness_core import settle
from test_harness_mcp import FakeHost
from test_mas_tasks import agent_up, down

SENTINEL = "LLM_0_SENTINEL_acceptance_key"
ENVS = E.load_environments()
LOCAL_URL = "http://127.0.0.1:8080/v1/chat/completions"
META_URL = "https://api.meta.ai/v1/chat/completions"
MODEL = "llama-3.1-8b-instruct-q4_k_m"


class Mem(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.d: dict = {}

    def set_password(self, s, u, p):
        self.d[(s, u)] = p

    def get_password(self, s, u):
        return self.d.get((s, u))

    def delete_password(self, s, u):
        del self.d[(s, u)]


class Dialog:
    """The owner at his own screen: approves (or not) what the harness shows. Every call is a human gesture; a test sets what he does."""

    def __init__(self):
        self.result, self.secret_value, self.shown = "approved", SENTINEL, []

    def confirm(self, title, message, **kw):
        self.shown.append(("confirm", message))
        return {"result": self.result, "mechanism": "fake_os_dialog", "strong": None, "assurance": "weak"}

    def secret(self, title, message, **kw):
        self.shown.append(("secret", message))
        return {"result": "entered" if self.result == "approved" else self.result, "mechanism": "fake_hidden_window", "value": self.secret_value if self.result == "approved" else None}


@dataclass
class Script:
    """What the played models say. `accuracy[(environment, access, task_type)]` = how many of an exam's tasks the model gets right (the oracle answers the suite's references); `answers` = (predicate on
    (environment, access, prompt), reply or callable) for everything else; `calls` records what each model was asked, by environment."""
    accuracy: dict = field(default_factory=dict)
    answers: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    fail: dict = field(default_factory=dict)  # (environment) -> http status the model's server answers with
    model_accuracy: dict = field(default_factory=dict)  # (served model id, task type) -> how many of an exam's tasks that model gets right (overrides `accuracy`)
    suites: dict = field(default_factory=dict)

    def exam_task(self, task_type, prompt):
        suite = self.suites.get(task_type) or self.suites.setdefault(task_type, load_suite(task_type))
        for n, t in enumerate((t for t in suite.tasks if t.counts)):
            if suite.prompt_for(t) == prompt:
                return n, t
        return None, None

    def reply(self, env, access, prompt, task_type=None, model=None) -> tuple[int, str]:
        self.calls.append((env, access, prompt[:80]))
        if env in self.fail:
            return self.fail[env], ""
        for tt in ([task_type] if task_type else []) + list({k[2] for k in self.accuracy} | {k[1] for k in self.model_accuracy}):
            n, t = self.exam_task(tt, prompt)
            if t is not None:
                k = self.model_accuracy.get((model, tt), self.accuracy.get((env, access, tt), 0))
                return 200, (t.reference if n < k else "no")
        for pred, out in self.answers:
            if pred(env, access, prompt):
                return 200, out(env, access, prompt) if callable(out) else out
        return 200, "an answer"


class World:
    def __init__(self):
        self.script = Script()
        self.dialog = Dialog()
        self.mem = Mem()

    async def __aenter__(self):
        self.old_kr = keyring.get_keyring()
        keyring.set_keyring(self.mem)
        return self

    async def __aexit__(self, *a):
        keyring.set_keyring(self.old_kr)

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        prompt = body["messages"][0]["content"]
        local = str(req.url) == LOCAL_URL
        env, access = ("llamacpp-local", "local-hardware") if local else ("meta-model-api", "meta-model-api:api-key")
        if not local:
            assert req.headers["authorization"] == f"Bearer {SENTINEL}"
        elif len(prompt) / 4 > 8192:  # the loopback server's own answer when the input does not fit its configured context
            self.script.calls.append((env, access, "context overflow"))
            return httpx.Response(400, text="context length exceeded")
        status, text = self.script.reply(env, access, prompt, model=body.get("model"))
        if status != 200:
            return httpx.Response(status, text="error")
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    def run_cli(self, argv, cwd, env, timeout_s, stdin_text=None):
        exe = (cwd and str(argv[0]).rsplit("/", 1)[-1]) or "opencode"
        access = "opencode:meta-api" if "metaapi/" in " ".join(argv) else exe + ":local"
        prompt = next((a for a in argv if a.startswith("Task:\n")), "")[len("Task:\n"):]
        status, text = self.script.reply(exe, access, prompt)
        return {"exit": 0 if status == 200 else 1, "stdout": text, "stderr": "boom" if status != 200 else "", "timed_out": False, "duration_s": 0.01}


async def build(tmp: Path, *, key: bool = True, server: bool = True, web: bool = True, subtypes=("local_model", "own_harness", "provider"), clis=None, second: bool = False, envs=None):
    """The coordinator, the Lenovo node, and an MCP host (Claude Desktop). Returns (world, c, nid, host, teardown)."""
    w = World()
    await w.__aenter__()
    cm = coordinator(tmp / "c", web=web)
    c = await cm.__aenter__()
    agent, tsk, nid = await agent_up(c, tmp, name="lenovo")
    if key:
        C.set("MODEL_API_KEY", SENTINEL)
    envs = envs or ENVS
    agent.runner.ctx.update({"envs": envs, "os_family": "windows", "find_exe": lambda n: "/bin/" + n, "sandbox_backends": [], "environ": {"PATH": "/usr/bin", "HOME": "/home/x"},
                             "transport": httpx.MockTransport(w.handler), "run": w.run_cli, "policy": E.load_policy(), "mas_home": tmp / "mas"})
    (tmp / "mas").mkdir(exist_ok=True)
    c.registry.save_analysis(nid, {"facts": {"machine.os.family": {"value": "windows", "status": "tested"}, "machine.os.name": {"value": "Windows 11", "status": "tested"},
                                              "machine.ai_clis": {"value": clis or [{"id": "opencode", "name": "OpenCode", "set_up": True}], "status": "tested"}, "machine.api_key_variable_names": {"value": [], "status": "tested"}}}, time.time())
    if server:
        set_server(c, nid, up=True)
    c.harness.native_confirm = w.dialog.confirm
    c.harness.native_secret = w.dialog.secret
    c.harness.raise_dialogs = False  # this world plays the owner through mas_request_approval; tests/test_harness_dialogs.py is where the harness opens the dialogs by itself
    c.services.envs = c.services.router.envs = c.services.facts.envs = envs  # the catalog this world runs on (the shipped one unless a test hands another)
    c.services.policy.open_subtypes = set(subtypes)
    from envrec_coordinator import harness as H
    H.osconfirm.available = lambda *a, **k: {"available": True, "mechanism": "fake_os_dialog"}
    tear2 = None
    if second:  # a second paired computer, with its own local model server: where a result is verified when the first one produced it
        agent2, tsk2, nid2 = await agent_up(c, tmp, name="second")
        agent2.runner.ctx.update(dict(agent.runner.ctx))
        c.registry.save_analysis(nid2, {"facts": {"machine.os.family": {"value": "windows", "status": "tested"}, "machine.ai_clis": {"value": [{"id": "opencode", "name": "OpenCode", "set_up": True}], "status": "tested"},
                                                   "machine.api_key_variable_names": {"value": [], "status": "tested"}}}, time.time())
        set_server(c, nid2, up=True)
        w.second = nid2
        tear2 = (agent2, tsk2)
    host = FakeHost(c, tmp, caps={})
    await host.start()
    local = FakeHost(c, tmp, caps={}, channel="terminal-local", client="fake-local-chat")
    await local.start()
    host.local = local  # a local chat (a model on this computer in the middle): the only chat a confidential task can come from, besides the owner panel
    await c.services.refresh_credentials()

    async def teardown():
        await local.stop()
        await host.stop()
        if tear2:
            await down(*tear2)
        await down(agent, tsk)
        await cm.__aexit__(None, None, None)
        await w.__aexit__()
    return w, c, nid, host, teardown


def set_server(c, nid, *, up: bool, model: str = MODEL) -> None:
    """The record a setup leaves: the pick, and the plan whose start step is done (the server runs) or undone (it was stopped)."""
    (c.runs.picks).mkdir(parents=True, exist_ok=True)
    (c.runs.setups).mkdir(parents=True, exist_ok=True)
    (c.runs.picks / f"{nid}.json").write_text(json.dumps({"run": "r1", "node": nid, "picked_at": 1.0, "option": {"id": "opt-" + model, "origin": "catalog_runtime", "model": model, "model_file": model, "build": {"sha256": hashlib.sha256(model.encode()).hexdigest()}}}))
    (c.runs.setups / f"{nid}.json").write_text(json.dumps({"run": "r1", "node": nid, "recorded_at": 1.0, "plan": {"plan_hash": "sha256:" + "11" * 32, "option_id": "opt-llama", "verification": {"endpoint": "127.0.0.1:8080"},
                                                                                                                       "steps": [{"n": 1, "op": "start_server"}, {"n": 2, "op": "minimal_call"}]},
                                                       "status": {"1": "done" if up else "undone", "2": "done"}}))  # stopping the server undoes its start step; the minimal call has nothing to undo


async def approve(host, c, job_or_intent: str | None = None, *, intent_id: str | None = None):
    """The owner presses Approve in his own system dialog (the tool asks; the dialog is the gesture)."""
    r = await host.tool("mas_request_approval", intent_id=intent_id)
    return r


async def wait_job(host, job_id, *, until=("done", "refused", "declined", "failed", "exhausted", "aborted", "route_changed", "done_unchecked", "unchecked"), timeout=60):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        r = await host.tool("mas_job", job_id=job_id)
        last = r
        if r["structuredContent"].get("state") in until:
            return r
        await asyncio.sleep(0.2)
    raise AssertionError(f"job {job_id} never finished: {last['content'][0]['text'][:300]}")
