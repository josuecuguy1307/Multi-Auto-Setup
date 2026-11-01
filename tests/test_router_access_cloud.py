"""`cloud` belongs to the ACCESS, not only to the environment: one environment (OpenCode) can run on a loopback model and, through another access, on an API that leaves the network. A confidential task
must never reach the second, whatever the environment says. SIMULATED-LOCAL: no process, no network."""
from __future__ import annotations

import copy

from envrec_coordinator.router import DictFacts, MachineFacts, Refusal, Router
from envrec_coordinator.router.facts import admission_key
from tests.test_router import ENVS, admission, machine, policy, task

REMOTE = {"id": "opencode:remote-api", "kind": "api_key", "cost_class": "api", "plan_group": None, "env_names": ["REMOTE_API_KEY"], "cloud": True}


def world(access_cloud_flag: bool | None = True):
    envs = {"opencode": copy.deepcopy(ENVS["opencode"])}  # only this environment, so what is chosen is about its accesses
    acc = dict(REMOTE)
    if access_cloud_flag is None:
        acc.pop("cloud")
    else:
        acc["cloud"] = access_cloud_flag
    envs["opencode"]["accesses"].append(acc)
    assert envs["opencode"]["cloud"] is False  # the environment itself is marked local: only the access leaves the network
    return envs


def router_for(envs, adm, sender=None):
    m = machine(env_names={"REMOTE_API_KEY"}, installed={"opencode"})
    log = []
    r = Router(envs=envs, facts=DictFacts([m], admission=dict(adm)), policy=policy(), log=lambda t, p, a: log.append((t, a, p)), sender=sender, payloads=lambda ref: "text")
    return r, log


def both_measured(ttype="code"):
    return [admission(ttype, "opencode"), admission(ttype, "opencode", access="opencode:remote-api", passed=10)]  # the remote access is even better measured


def test_a_confidential_task_never_keeps_an_access_that_leaves_the_network_inside_a_local_environment():
    r, _ = router_for(world(), both_measured())
    d = r.route(task(ttype="code", sens="confidential", chat="local"))
    assert d.route.access_id == "opencode:local"
    first = d.rule_trace[0]
    assert first.step == "sensitivity" and first.values.get("opencode|qwen3-8b|opencode:remote-api|mac") == "cloud"
    assert "opencode|qwen3-8b|opencode:remote-api|mac" not in first.candidates_out


def test_without_the_local_access_the_confidential_task_is_refused_not_sent_to_the_remote_one():
    envs = world()
    envs["opencode"]["accesses"] = [a for a in envs["opencode"]["accesses"] if a["id"] != "opencode:local"]
    r, _ = router_for(envs, [admission("code", "opencode", access="opencode:remote-api", passed=10)])
    out = r.route(task(ttype="code", sens="confidential", chat="local"))
    assert isinstance(out, Refusal) and out.code in ("no_local_candidate", "provisional_only")


def test_a_public_task_may_use_the_remote_access_and_the_cost_class_still_orders_it_after_the_local_one():
    r, _ = router_for(world(), both_measured())
    d = r.route(task(ttype="code", sens="public", chat="local"))
    assert d.route.access_id == "opencode:local"
    assert any(s.step == "cost_class" and "opencode|qwen3-8b|opencode:remote-api|mac" in s.values for s in d.rule_trace)


def test_an_access_without_its_own_flag_inherits_the_environments():
    envs = world(access_cloud_flag=None)
    r, _ = router_for(envs, both_measured())
    d = r.route(task(ttype="code", sens="confidential", chat="local"))
    assert d.route.access_id == "opencode:local"  # the environment is local, so an unflagged access is local: the rule is data, not a guess
    assert "opencode|qwen3-8b|opencode:remote-api|mac" in d.rule_trace[0].candidates_out


def test_dispatch_refuses_a_confidential_input_for_a_cloud_access_even_if_a_decision_got_there():
    class Sender:
        called = False

        def start_environment_run(self, *a, **k):
            Sender.called = True
            raise AssertionError("must not be reached")

    r, _ = router_for(world(), [admission("code", "opencode", passed=5), admission("code", "opencode", access="opencode:remote-api", passed=10)], sender=Sender())
    d = r.route(task("t9", ttype="code", sens="public", chat="local"))
    assert d.route.access_id == "opencode:remote-api"  # public: the local access failed the exam, so the remote one is the admitted one
    r._state["t9"]["decision"] = d2 = d.model_copy(update={"effective_sensitivity": "confidential"})
    out = r.dispatch(d2)
    assert isinstance(out, Refusal) and out.code == "confidential_cloud_environment" and Sender.called is False


# ---- the node says no too (defense in depth) and the signed scope names the access ---------------------------------------------------------------------------------------------------------

def _node_side(access_id, sensitivity="confidential", scope_access=None):
    from envrec_net import envrun as E
    from envrec_net import task as T
    from envrec_node import envrun as node_envrun
    envs = world()
    entry, mode = envs["opencode"], E.mode_of(envs["opencode"], "code")
    params = T.validate_params("environment_run", {"environment_id": "opencode", "task_type": "code", "sensitivity": sensitivity, "mode": mode["name"], "workspace_id": "w1", "recipe_sha256": E.recipe_sha256(entry),
                                                   "input_text": "x", "endpoint": "127.0.0.1:8080", "model_id": "m", "access_id": access_id})
    scope = E.env_scope(entry=entry, task_type="code", sensitivity=sensitivity, workspace_id="w1", endpoint="127.0.0.1:8080", origin_event="evt", decision={}, access_id=scope_access if scope_access is not None else access_id)
    ctx = {"envs": envs, "os_family": "darwin", "sandbox_backends": [], "find_exe": lambda n: "/bin/opencode"}
    return node_envrun.authorize, params, {"scope": scope}, ctx


def test_the_node_refuses_a_confidential_run_through_a_cloud_access():
    import pytest
    from envrec_net import task as T
    authorize, params, token, ctx = _node_side("opencode:remote-api")
    with pytest.raises(T.TokenRejected) as e:
        authorize(params, token, ctx)
    assert str(e.value) == "confidential_cloud" or getattr(e.value, "reason", "") == "confidential_cloud"


def test_the_node_accepts_the_same_confidential_run_through_the_local_access():
    authorize, params, token, ctx = _node_side("opencode:local")
    entry, _ = authorize(params, token, ctx)
    assert entry["id"] == "opencode"


def test_a_token_signed_for_one_access_does_not_authorize_another():
    import pytest
    from envrec_net import task as T
    authorize, params, token, ctx = _node_side("opencode:remote-api", sensitivity="public", scope_access="opencode:local")
    with pytest.raises(T.TokenRejected):
        authorize(params, token, ctx)


def test_an_environment_run_without_an_access_is_treated_as_cloud_when_any_access_of_the_environment_is():
    from envrec_net import envrun as E
    envs = world()
    assert E.is_cloud(envs["opencode"], None) is True and E.is_cloud(envs["opencode"], "opencode:local") is False and E.is_cloud(envs["opencode"], "no-such-access") is True
    assert E.is_cloud({"id": "x", "cloud": False, "accesses": [{"id": "a"}]}, None) is False
