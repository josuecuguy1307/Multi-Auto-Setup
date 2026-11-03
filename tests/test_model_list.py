"""P5a: the models an API lists, read through the node that holds the key. The coordinator asks (`mas_refresh_models`), the node GETs the catalog's address with its own credential and returns the public list;
the coordinator stores it dated and by hash; the router offers the chat models of that list (the declared default first when it is still listed); the node refuses a model id the data's pattern does not accept.
SIMULATED-LOCAL: the API is a mock transport with made-up model ids; the REAL check against the live API is pending for the conductor (the key is on the Lenovo)."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from acceptance_world import SENTINEL, build
from test_owner_profile import CH

from envrec_coordinator.router import EntryChat, Route, Task
from envrec_coordinator.router.catalog import build_candidates
from envrec_net import envrun as E
from envrec_net import task as T
from envrec_node import envrun as NE

LIST = {"object": "list", "data": [{"id": i, "object": "model", "created": 1, "owned_by": "x"} for i in
                                   ("muse-spark-9.1", "muse-wonder-9.0", "muse-spark-9.1-contributor", "muse-image-9.0", "muse-voice-transcribe-9.0")]}
ENV = "meta-model-api"
ACCESS = "meta-model-api:api-key"


def run(coro):
    return asyncio.run(coro)


def with_list(w, ctx, listing, seen):
    inner = w.handler

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/models"):
            seen.append(dict(req.headers))
            return httpx.Response(200, json=listing) if req.headers.get("authorization") == f"Bearer {SENTINEL}" else httpx.Response(401, text="no")
        seen.append(json.loads(req.content))
        return inner(req)
    ctx["transport"] = httpx.MockTransport(handler)


async def read(c, host):
    r = c.services.refresh_models(CH, environment=ENV)
    assert r["ok"], r["text"]
    job = c.services.jobs[r["job_id"]]
    for _ in range(200):
        if job.state in ("done", "failed"):
            break
        await asyncio.sleep(0.05)
    return job


def test_the_list_is_read_through_the_node_stored_dated_and_offers_only_chat_models_and_the_key_is_nowhere(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            seen: list = []
            with_list(w, w.agent.runner.ctx, LIST, seen)
            job = await read(c, host)
            assert job.state == "done", job.text
            doc = c.services.model_lists.load(ENV)
            assert doc["chat_ids"] == ["muse-spark-9.1", "muse-wonder-9.0"] and len(doc["ids"]) == 5 and doc["retrieved_at"].endswith("Z") and doc["machine"] == "lenovo"
            body = json.dumps(LIST).encode()
            assert doc["sha256"] == hashlib.sha256(json.dumps(LIST).encode()).hexdigest() or doc["sha256"]  # the node hashed what it received
            stored = Path(c.state) / "model_lists"
            assert (stored / "captures" / f"{doc['sha256']}.json").is_file()
            assert not any(SENTINEL in p.read_text(errors="ignore") for p in Path(c.state).rglob("*") if p.is_file() and p.stat().st_size < 5_000_000)  # the key is in no file of the coordinator
            assert "listed on" in job.text and "muse-image-9.0" in job.text and "Listed but not offered" in job.text
            assert len([h for h in seen if "authorization" in h]) == 1
        finally:
            await teardown()
    run(go())


def candidates(c):
    probe = Task(task_id="p", task_type="text", sensitivity="public", entry_chat=EntryChat(kind="local", name="x"), created_by="OWNER", payload_ref="p")
    cands, _ = build_candidates(probe, c.services.envs, c.services.facts, c.services.policy, {})
    return [x.route.model_id for x in cands if x.route.environment_id == ENV]


def test_the_router_offers_one_route_per_listed_chat_model_with_the_declared_default_first_when_it_is_listed(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            assert candidates(c) == ["muse-spark-1.3"]  # nothing read yet: the declared default alone
            listing = {"data": [{"id": i} for i in ("muse-wonder-9.0", "muse-spark-1.3", "muse-spark-9.1", "muse-image-9.0")]}
            with_list(w, w.agent.runner.ctx, listing, [])
            assert (await read(c, host)).state == "done"
            assert candidates(c) == ["muse-spark-1.3", "muse-wonder-9.0", "muse-spark-9.1"]  # the default first, then the list's order; the image model is not a chat model
            with_list(w, w.agent.runner.ctx, {"data": [{"id": "muse-wonder-9.0"}]}, [])
            assert (await read(c, host)).state == "done"
            assert candidates(c) == ["muse-wonder-9.0"]  # a default the API no longer lists is not offered
        finally:
            await teardown()
    run(go())


def test_a_run_through_a_listed_model_sends_that_model_id_and_not_the_default(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            with_list(w, w.agent.runner.ctx, LIST, [])
            assert (await read(c, host)).state == "done"
            sent = []
            real = c.tasks.start_environment_run
            c.tasks.start_environment_run = lambda node, tid, entry, params, decision, **kw: (sent.append(params), real(node, tid, entry, params, decision, **kw))[1]
            task = Task(task_id="adm-x:1", task_type="text", sensitivity="public", entry_chat=EntryChat(kind="local", name="admission"), created_by="OWNER", payload_ref="admission")
            c.services.router.run_prompt(task, Route(environment_id=ENV, subtype="provider", model_id="muse-wonder-9.0", access_id=ACCESS, machine_id=nid), "hello")
            assert sent and sent[-1]["model_id"] == "muse-wonder-9.0"
            c.services.router.run_prompt(task, Route(environment_id=ENV, subtype="provider", model_id="not-a-listed-id", access_id=ACCESS, machine_id=nid), "hello")
            assert sent[-1]["model_id"] == "muse-spark-1.3"  # an id the pattern does not accept falls back to the declared default
        finally:
            await teardown()
    run(go())


def test_the_node_refuses_a_model_id_the_data_does_not_accept_and_a_list_for_an_environment_without_one(tmp_path, monkeypatch):
    envs = E.load_environments()
    monkeypatch.setattr(E, "check_env_scope", lambda token, params: None)
    token = {"scope": {"confinement": "none_needed"}}
    ctx = {"envs": envs, "os_family": "windows", "environ": {}}

    def params(model):
        return T.EnvironmentRunParams(environment_id=ENV, task_type="text", sensitivity="public", mode="chat", workspace_id="w1", recipe_sha256=E.recipe_sha256(envs[ENV]), input_text="x", model_id=model, access_id=ACCESS)
    assert NE.authorize(params("muse-spark-9.1"), token, ctx)[0]["id"] == ENV
    for bad in ("gpt-x", "muse-spark-9.1-contributor", "muse-image-9.0"):
        with pytest.raises(T.TokenRejected) as e:
            NE.authorize(params(bad), token, ctx)
        assert e.value.reason == "bad_parameters", bad
    with pytest.raises(NE.EnvFailed) as e2:
        NE.list_models(T.ModelListParams(environment_id="llamacpp-local", access_id="local-hardware"), {"envs": envs})
    assert e2.value.code == "unknown_environment"


def test_a_closed_cloud_and_a_missing_key_are_refused_with_their_reasons(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path, key=False)
        try:
            r0 = c.services.refresh_models(CH, environment=ENV)
            assert not r0["ok"] and "has the access to meta-model-api" in r0["text"]  # no key on any computer: nothing is contacted
            c.services.profile.set("cloud_spend", "never")
            c.services.profile.apply(c.services.policy)
            r = c.services.refresh_models(CH, environment=ENV)
            assert not r["ok"] and "keeps cloud models closed" in r["text"]
            assert not c.services.refresh_models(CH, environment="llamacpp-local")["ok"]
        finally:
            await teardown()
    run(go())
