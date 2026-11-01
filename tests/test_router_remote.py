"""The Meta Model API and OpenCode-through-the-API as DATA: an adapter that names its own https address and its credential by NAME, an access that carries its own adapter, typed reasons for an offline
machine and a missing credential, and N5 (the loopback model server) unchanged. SIMULATED-LOCAL: an httpx mock stands for the API; nothing leaves this Mac."""
from __future__ import annotations

import copy
import json

import httpx
import pytest

from envrec_coordinator.router import DictFacts, Refusal, Router
from envrec_net import envrun as E
from envrec_net import task as T
from envrec_node import envrun as node_envrun
from tests.test_router import ENVS, admission, machine, policy, task

SENTINEL = "LLM_0_SENTINEL_never_shown_anywhere"
KEY_WORLD = dict(env_names={"MODEL_API_KEY"}, installed={"opencode"}, model_endpoint="127.0.0.1:8080", model_id="llama-3.1-8b-instruct-q4_k_m")


def measured(ttype, env, model, access, passed=9):
    """An admission fact for a combination whose model id the data names (the shared helper writes `default` for a provider)."""
    return f"admission.{ttype}.mac.{env}.{model}.{access}", {"status": "tested", "graded_by": "code", "passed": passed, "of": 10, "identity": {}}


def pol(**kw):
    return policy(approved_providers_for_internal=set(), **kw)


def router(machines=None, adm=(), **kw):
    log = []
    return Router(envs=ENVS, facts=DictFacts(machines or [machine(**KEY_WORLD)], admission=dict(adm)), policy=pol(), log=lambda t, p, a: log.append((t, a, p)), **kw), log


# ---- the catalog is data and is checked -------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_shipped_catalog_passes_its_own_safety_check_and_has_the_new_rows():
    assert E.check_catalog(ENVS) == []
    meta = ENVS["meta-model-api"]
    assert (meta["subtype"], meta["cloud"], meta["model"], meta["adapter"]["base_url"], meta["adapter"]["chat_path"]) == ("provider", True, "muse-spark-1.3", "https://api.meta.ai/v1", "/chat/completions")
    assert meta["accesses"][0]["cost_class"] == "api" and meta["accesses"][0]["env_names"] == ["MODEL_API_KEY"] and meta["accesses"][0]["cloud"] is True
    oc = {a["id"]: a for a in ENVS["opencode"]["accesses"]}
    assert E.is_cloud(ENVS["opencode"], "opencode:local") is False and E.is_cloud(ENVS["opencode"], "opencode:meta-api") is True and oc["opencode:meta-api"]["cost_class"] == "api"


def test_an_access_adapter_is_laid_over_the_environments_and_changes_the_recipe_hash():
    oc = ENVS["opencode"]
    local, remote = E.adapter_of(oc, "opencode:local"), E.adapter_of(oc, "opencode:meta-api")
    assert local["needs_model_endpoint"] is True and remote["needs_model_endpoint"] is False and "metaapi" in remote["env_set"]["OPENCODE_CONFIG_CONTENT"] and "llamacpp" in local["env_set"]["OPENCODE_CONFIG_CONTENT"]
    assert E.mode_of(oc, "code", "opencode:meta-api")["argv"][4] == "metaapi/{model}" and E.mode_of(oc, "code", "opencode:local")["argv"][4] == "llamacpp/{model}"
    changed = copy.deepcopy(oc)
    changed["accesses"][-1]["adapter"]["env_pass"] = ["SOMETHING_ELSE"]
    assert E.recipe_sha256(changed) != E.recipe_sha256(oc)


@pytest.mark.parametrize("edit,word", [
    (lambda e: e["adapter"].update(base_url="http://api.example.test/v1"), "https"),
    (lambda e: e["accesses"][0].update(cloud=False), "cloud: true"),
    (lambda e: e["adapter"].update(needs_model_endpoint=True), "loopback model endpoint"),
])
def test_a_remote_adapter_that_is_not_https_not_cloud_or_needs_the_loopback_server_is_refused(edit, word):
    envs = copy.deepcopy(ENVS)
    edit(envs["meta-model-api"])
    problems = E.check_catalog(envs)
    assert any(word in p for p in problems), problems


def test_a_cloud_access_without_a_base_url_on_a_local_http_adapter_is_refused():
    envs = copy.deepcopy(ENVS)
    envs["llamacpp-local"]["accesses"][0]["cloud"] = True  # the loopback server marked cloud with no address of its own
    assert any("must name its base_url" in p for p in E.check_catalog(envs))


def test_a_catalog_that_fails_the_check_is_not_loaded(tmp_path):
    doc = {"environments": [copy.deepcopy(ENVS["meta-model-api"])]}
    doc["environments"][0]["adapter"]["base_url"] = "http://plain.example.test/v1"
    f = tmp_path / "environments.json"
    f.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="unsafe"):
        E.load_environments(f)


# ---- the router: candidates, filters, typed reasons ---------------------------------------------------------------------------------------------------------------------------------

def test_a_public_task_lists_the_meta_api_and_opencode_through_it_with_the_models_the_data_names():
    d = router(adm=[measured("text", "meta-model-api", "muse-spark-1.3", "meta-model-api:api-key")])[0].route(task(ttype="text", sens="public", chat="local"))
    keys = d.rule_trace[0].candidates_in
    assert "meta-model-api|muse-spark-1.3|meta-model-api:api-key|mac" in keys and "opencode|muse-spark-1.3|opencode:meta-api|mac" in keys and "opencode|llama-3.1-8b-instruct-q4_k_m|opencode:local|mac" in keys


def test_a_confidential_task_cuts_both_api_routes_as_cloud_and_keeps_the_local_ones():
    adm = [admission("summarize", "llamacpp-local", model="llama-3.1-8b-instruct-q4_k_m", passed=9)]
    d = router(adm=adm)[0].route(task(ttype="summarize", sens="confidential", chat="local"))
    cut = d.rule_trace[0].values
    assert cut["meta-model-api|muse-spark-1.3|meta-model-api:api-key|mac"] == "cloud" and cut["opencode|muse-spark-1.3|opencode:meta-api|mac"] == "cloud"
    assert d.route.environment_id == "llamacpp-local" and d.route.model_id == "llama-3.1-8b-instruct-q4_k_m"


def test_without_the_variable_the_reason_names_it_and_never_a_value():
    m = machine(**{**KEY_WORLD, "env_names": set(), "model_endpoint": None, "installed": set()})
    out = Router(envs=ENVS, facts=DictFacts([m]), policy=pol()).route(task(ttype="text", sens="public", chat="local"))
    assert isinstance(out, Refusal) and out.code == "no_candidate"
    assert out.details["environments"]["meta-model-api@mac"] == "access missing: MODEL_API_KEY"
    assert SENTINEL not in json.dumps(out.model_dump())


def test_an_offline_machine_is_a_typed_refusal_with_no_dispatch_and_no_retry():
    sent = []

    class Sender:
        def start_environment_run(self, *a, **k):
            sent.append(a)

    m = machine(**{**KEY_WORLD})
    m.online = False
    r, log = router(machines=[m], sender=Sender(), payloads=lambda ref: "text")
    out = r.route(task(ttype="text", sens="public", chat="local"))
    assert isinstance(out, Refusal) and out.code == "machine_offline" and "machine offline" in out.reason
    assert set(out.details["codes"].values()) == {"machine_offline"} and all(v == "machine offline" for v in out.details["environments"].values())
    assert sent == [] and [t for t, _, _ in log] == ["route_refusal"]


def test_a_dispatch_to_the_api_carries_no_endpoint_the_data_named_model_and_the_access():
    seen = {}

    class Sender:
        tasks: dict = {}

        def start_environment_run(self, node, tid, entry, params, decision):
            seen.update(params)

            class Rec:
                id, status, reason = "n1", "sent", None
            return Rec()

    r, _ = router(sender=Sender(), payloads=lambda ref: "the input", adm=[measured("text", "meta-model-api", "muse-spark-1.3", "meta-model-api:api-key", 10)])
    d = r.route(task(ttype="text", sens="public", chat="local"))
    assert d.route.environment_id == "meta-model-api"  # measured first: the one measured candidate
    r.dispatch(d)
    assert (seen["endpoint"], seen["model_id"], seen["access_id"], seen["environment_id"]) == (None, "muse-spark-1.3", "meta-model-api:api-key", "meta-model-api")


# ---- the node: the call, the credential, N5 ------------------------------------------------------------------------------------------------------------------------------------------------

def _params(env, access, ttype="text", sens="public", endpoint=None, model="muse-spark-1.3"):
    entry, mode = ENVS[env], E.mode_of(ENVS[env], ttype, access)
    return T.validate_params("environment_run", {"environment_id": env, "task_type": ttype, "sensitivity": sens, "mode": mode["name"], "workspace_id": "w1", "recipe_sha256": E.recipe_sha256(entry),
                                                 "input_text": "Summarize: the sky is blue.", "endpoint": endpoint, "model_id": model, "access_id": access})


def _ctx(tmp_path, handler, secret=lambda n: SENTINEL if n == "MODEL_API_KEY" else None):
    (tmp_path / "mas").mkdir(exist_ok=True)
    return {"envs": ENVS, "os_family": "darwin", "sandbox_backends": [], "mas_home": tmp_path / "mas", "environ": {}, "secret": secret, "transport": httpx.MockTransport(handler), "policy": E.load_policy()}


def test_the_api_call_goes_to_the_documented_address_with_the_bearer_and_only_the_documented_fields(tmp_path):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        return httpx.Response(200, json={"choices": [{"message": {"content": "a short summary"}}]})
    entry = ENVS["meta-model-api"]
    p = _params("meta-model-api", "meta-model-api:api-key")
    result, evidence = node_envrun.run_environment(p, entry, E.mode_of(entry, "text", "meta-model-api:api-key"), _ctx(tmp_path, handler))
    req = calls[0]
    assert str(req.url) == "https://api.meta.ai/v1/chat/completions" and req.headers["authorization"] == f"Bearer {SENTINEL}"
    assert json.loads(req.content) == {"messages": [{"role": "user", "content": "Summarize: the sky is blue."}], "model": "muse-spark-1.3"}
    assert result["output_text"] == "a short summary" and SENTINEL not in json.dumps([result, evidence])


def test_a_missing_credential_fails_typed_and_makes_no_call(tmp_path):
    calls = []
    entry = ENVS["meta-model-api"]
    with pytest.raises(node_envrun.EnvFailed) as e:
        node_envrun.run_environment(_params("meta-model-api", "meta-model-api:api-key"), entry, E.mode_of(entry, "text", "meta-model-api:api-key"),
                                    _ctx(tmp_path, lambda r: calls.append(r) or httpx.Response(200), secret=lambda n: None))
    assert e.value.code == "access_missing" and "MODEL_API_KEY" in str(e.value) and calls == []


def test_an_http_failure_is_reported_without_the_key_or_the_body(tmp_path):
    entry = ENVS["meta-model-api"]
    with pytest.raises(node_envrun.EnvFailed) as e:
        node_envrun.run_environment(_params("meta-model-api", "meta-model-api:api-key"), entry, E.mode_of(entry, "text", "meta-model-api:api-key"),
                                    _ctx(tmp_path, lambda r: httpx.Response(401, text=f"bad key {SENTINEL}")))
    assert "401" in str(e.value) and SENTINEL not in str(e.value)


def _authorize(params, access, tmp_path=None):
    scope = E.env_scope(entry=ENVS[params.environment_id], task_type=params.task_type, sensitivity=params.sensitivity, workspace_id="w1", endpoint=params.endpoint, origin_event="evt", decision={}, access_id=access)
    return node_envrun.authorize(params, {"scope": scope}, {"envs": ENVS, "os_family": "darwin", "sandbox_backends": [], "find_exe": lambda n: "/bin/x"})


def test_an_api_access_takes_no_endpoint_and_a_local_one_still_needs_a_loopback_literal():
    with pytest.raises(T.TokenRejected) as e:
        _authorize(_params("meta-model-api", "meta-model-api:api-key", endpoint="127.0.0.1:8080"), "meta-model-api:api-key")
    assert "bad_parameters" in str(e.value) or getattr(e.value, "reason", "") == "bad_parameters"
    _authorize(_params("meta-model-api", "meta-model-api:api-key"), "meta-model-api:api-key")  # no endpoint: accepted
    for bad in ("localhost:8080", "192.168.1.5:8080", "0.0.0.0:8080"):  # N5 for the node's own model server
        with pytest.raises(T.TokenRejected):
            _authorize(_params("llamacpp-local", "local-hardware", endpoint=bad, model="m"), "local-hardware")
        with pytest.raises(T.TokenRejected):
            _authorize(_params("opencode", "opencode:local", ttype="text", endpoint=bad, model="m"), "opencode:local")
    _authorize(_params("opencode", "opencode:local", ttype="text", endpoint="127.0.0.1:8080", model="m"), "opencode:local")


def test_opencode_through_the_api_passes_the_key_by_name_into_its_process_only(tmp_path):
    seen = {}

    def runner(argv, cwd, env, timeout_s, stdin_text=None):
        seen.update(argv=argv, env=dict(env))
        return {"exit": 0, "stdout": "done", "stderr": "", "timed_out": False, "duration_s": 0.1}
    entry = ENVS["opencode"]
    ctx = _ctx(tmp_path, lambda r: httpx.Response(500))
    ctx.update(run=runner, find_exe=lambda n: "/bin/opencode")
    p = _params("opencode", "opencode:meta-api", ttype="text")
    node_envrun.run_environment(p, entry, E.mode_of(entry, "text", "opencode:meta-api"), ctx)
    assert seen["env"]["MODEL_API_KEY"] == SENTINEL and "metaapi" in seen["env"]["OPENCODE_CONFIG_CONTENT"] and "muse-spark-1.3" in seen["env"]["OPENCODE_CONFIG_CONTENT"]
    assert SENTINEL not in seen["env"]["OPENCODE_CONFIG_CONTENT"] and "{env:MODEL_API_KEY}" in seen["env"]["OPENCODE_CONFIG_CONTENT"]  # the config names the variable; the value is only in the environment
    assert "metaapi/muse-spark-1.3" in seen["argv"]


# ---- the OpenCode install recipe is pinned data (nothing runs it yet) -----------------------------------------------------------------------------------------------------------------

def test_the_opencode_install_recipe_is_pinned_for_every_os_and_architecture_the_data_names():
    import re
    from urllib.parse import urlparse
    oc = ENVS["opencode"]
    inst, assets = oc["install"], oc["upstream"]["assets"]
    assert oc["upstream"]["release"] == "v1.19.5" and inst["sequence"] == ["fetch_check", "unpack_archive", "verify_program"]
    for fam, archs in inst["asset_keys"].items():
        assert fam in inst["binary"] and fam in inst["verify"]["command"]
        for arch, key in archs.items():
            a = assets[key]
            u = urlparse(a["url"])
            assert u.scheme == "https" and u.hostname == "github.com" and f"/{oc['upstream']['release']}/" in u.path and a["url"].endswith(a["name"]), (fam, arch)
            assert re.fullmatch(r"[0-9a-f]{64}", a["sha256"]) and isinstance(a["bytes"], int) and a["bytes"] > 0
    assert len({a["sha256"] for a in assets.values()}) == len(assets) == 6
    assert "DATA ONLY" in inst["status"]
