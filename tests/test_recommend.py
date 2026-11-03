"""The recommendation for work no catalog type names (slice B): sensitivity first, the written rule picks the default and the runner-up, a subscription tool waits for the owner's clearance (his decision,
recorded, undoable), research leads are shown and never proposed as the default, ONE chat question bound to the plan hash, and nothing is created until he answers. SIMULATED-LOCAL: the real router and
policy over fake facts; the world tests at the end drive the real coordinator, node and MCP server."""
from __future__ import annotations

import asyncio
import json

from acceptance_world import build
from test_acceptance_run import sc, text

from envrec_coordinator import recommend as R
from envrec_coordinator.harness import CHANNELS
from envrec_coordinator.profile import Profile
from envrec_coordinator.router import DictFacts, Router
from envrec_net import envrun as E
from tests.test_router import ENVS, admission, machine, policy

CH_CLOUD = CHANNELS["claude-desktop"]
CH_LOCAL = CHANNELS["terminal-local"]


class Log:
    def __init__(self):
        self.rows = []

    def add(self, type, payload=None, **kw):
        self.rows.append((type, payload, kw))


class Harness:
    def __init__(self):
        self.asked = []

    def ask_chat(self, run_id, *, key, text, options, on_choice, sensitivity="internal", multi=False):
        class I:
            id = "i-" + str(len(self.asked))
            text_fallback = text + "\n" + "\n".join(o["id"] for o in options)
        self.asked.append({"key": key, "text": text, "options": options, "on_choice": on_choice})
        return I


class NoModel:
    dir = None

    @staticmethod
    def model_status():
        return {"available": False, "reason": "no model"}


class Coord:
    def __init__(self):
        self.log, self.harness, self.runs = Log(), Harness(), NoModel()


class Svc(R.RecommendTools):
    """Just enough of Services for the recommendation: the real router, policy and catalog; fake facts."""

    def __init__(self, machines, *, catalog=None, adm=None, pol=None, profile=None):
        self.catalog = catalog or ENVS
        self.envs = E.runnable(self.catalog)
        self.policy = pol or policy(automated_use_cleared=set())
        self.facts = DictFacts(machines, admission=dict(adm or []))
        from envrec_coordinator.router import MemoryOutputs
        self.outputs = MemoryOutputs()
        self.router = Router(envs=self.envs, facts=self.facts, policy=self.policy, outputs=self.outputs, evidence={"entries": []})
        self.coord = Coord()
        self.profile = profile
        self.created = []

    def _chat(self, ch):
        from envrec_coordinator.router import EntryChat
        return EntryChat(kind="cloud" if ch.cloud else "local", name=ch.name)

    def _name(self, mid):
        return mid

    def _key_text(self, key):
        return key.replace("|", " / ")

    @staticmethod
    def _out(ok, text, **data):
        return {"ok": ok, "text": text, **data}

    def on_clearance(self, ident, option):
        self.policy.automated_use_cleared |= {ident}
        self.coord.log.add("automated_use_cleared", {"id": ident})
        return "cleared"

    def _create(self, ch, **kw):
        self.created.append(kw)
        return {"ok": True, "text": "task created"}


def local_world(**kw):
    return machine(installed={"opencode", "pi", "claude-code"}, signed_in={"claude-code:plan"}, **kw)


def test_the_default_is_what_the_written_rule_picks_and_the_runner_up_what_it_picks_without_that_environment():
    s = Svc([local_world()], pol=policy(automated_use_cleared={"claude-code"}))
    p = s.build_proposal(CH_LOCAL, domains=["grant writing"], sensitivity="public")
    assert [o.id for o in p.options][:2] == ["use_default", "use_runner"] and p.options[0].env != p.options[1].env
    assert p.options[0].cost_class == "local" and p.options[0].task_type == "text" and p.unclassified and p.refusal is None
    direct = s.router.explain(s._rec_task(CH_LOCAL, "text", ["grant writing"], "public", "x"))
    assert p.options[0].route_key == direct.route.key  # the same router, the same rule: nobody ranked anything for the recommendation
    assert "evidence: none" in p.options[0].evidence


def test_a_confidential_task_never_sees_a_cloud_access_and_no_clearance_is_offered_for_one():
    s = Svc([local_world()], adm=[admission("text", "llamacpp-local", passed=9)], pol=policy(automated_use_cleared=set()))
    p = s.build_proposal(CH_LOCAL, domains=["legal"], sensitivity="confidential")
    assert [o.env for o in p.options] == ["llamacpp-local"] and not any(o.cloud for o in p.options) and not any(o.kind == "clear_and_use" for o in p.options)
    unmeasured = Svc([local_world()], pol=policy(automated_use_cleared=set())).build_proposal(CH_LOCAL, domains=["legal"], sensitivity="confidential")
    assert unmeasured.options == [] and unmeasured.refusal.code == "provisional_only"  # nothing is measured here: confidential waits for a measurement, it is not guessed
    body = Svc([local_world()]).recommendation_text(unmeasured)
    assert "Nothing can be proposed" in body and "mas_run_admission" in body and "llamacpp-local" in body and "Plan " not in body  # what would unblock it is named; there is no question when there is nothing to choose
    assert "claude-code" not in body and "allow it with mas_profile" not in body  # nothing about a cloud tool is offered for a confidential request


def test_a_subscription_tool_waits_for_the_owners_clearance_and_the_option_says_so():
    m = machine(installed={"claude-code"}, signed_in={"claude-code:plan"}, model_endpoint=None)
    s = Svc([m], pol=policy(open_subtypes={"provider"}, automated_use_cleared=set()))
    p = s.build_proposal(CH_CLOUD, domains=["data science"], sensitivity="public")
    assert [(o.id, o.kind, o.env) for o in p.options] == [("clear_default", "clear_and_use", "claude-code")] and p.options[0].cost_class == "subscription"
    assert "once you allow it" in s.recommendation_text(p) and "yours to read" in s.recommendation_text(p)
    assert "claude-code" not in s.policy.automated_use_cleared  # asking is not clearing


def test_the_question_is_one_chat_choice_bound_to_the_plan_hash_and_a_stale_answer_is_not_used():
    s = Svc([local_world()], pol=policy(automated_use_cleared={"claude-code"}))
    out = s.recommend(CH_LOCAL, text="THE SECRET TASK TEXT", description="grant writing", domain=None, sensitivity="public")
    (q,) = s.coord.harness.asked
    h = out["plan_hash"]
    assert out["question"] is True and q["key"] == f"recommend:{h}" and h in q["text"] and [o["id"] for o in q["options"]] == ["use_default", "use_runner", "neither"]
    assert "THE SECRET TASK TEXT" not in q["text"]  # the question is about the kind of work, never its text
    s.facts._m[0].installed.discard("opencode")  # the world changed while the question was open
    reply = q["on_choice"]("use_default")
    assert "facts changed" in reply and s.created == [] and len(s.coord.harness.asked) == 2
    q2 = s.coord.harness.asked[1]
    reply2 = q2["on_choice"]("use_default")
    assert len(s.created) == 1 and s.created[0]["only_env"] and s.created[0]["task_type"] == "text" and s.created[0]["domain"] == ["grant writing"]
    answered = [r for r in s.coord.log.rows if r[0] == "recommendation_answered"]
    assert answered[-1][1]["gesture"] == "chat_choice" and answered[-1][1]["tag"] == "OWNER" and answered[-1][2] == {"owner": True} and "Recorded" in reply2


def test_choosing_to_clear_records_the_decision_then_creates_the_task_and_neither_creates_nothing():
    m = machine(installed={"claude-code"}, signed_in={"claude-code:plan"}, model_endpoint=None)
    s = Svc([m], pol=policy(open_subtypes={"provider"}, automated_use_cleared=set()))
    s.recommend(CH_CLOUD, text="t", description="data science", domain=["data science"], sensitivity="public")
    s.coord.harness.asked[0]["on_choice"]("clear_default")
    assert "claude-code" in s.policy.automated_use_cleared and any(r[0] == "automated_use_cleared" for r in s.coord.log.rows) and s.created[0]["only_env"] == "claude-code"
    s2 = Svc([m], pol=policy(open_subtypes={"provider"}, automated_use_cleared=set()))
    s2.recommend(CH_CLOUD, text="t", description="data science", domain=None, sensitivity="public")
    assert "no task was created" in s2.coord.harness.asked[0]["on_choice"]("neither") and s2.created == [] and not s2.policy.automated_use_cleared


def test_research_leads_are_shown_as_unverified_and_never_become_an_option():
    lead = {"id": "finlead", "title": "A finance harness", "subtype": "own_harness", "domains": ["finance"], "license": {"spdx": "MIT"}, "evidence_state": "hypothesis", "origin": "researched",
            "source_url": "https://github.com/example/finlead", "retrieved": "2026-11-02"}
    s = Svc([local_world()], catalog={**ENVS, "finlead": lead}, pol=policy(automated_use_cleared=set()))
    p = s.build_proposal(CH_LOCAL, domains=["finance"], sensitivity="public")
    assert [x["id"] for x in p.leads] == ["finlead"] and "finlead" not in {o.env for o in p.options}
    text_ = s.recommendation_text(p)
    assert "unverified, never proposed as the default" in text_ and "https://github.com/example/finlead" in text_
    assert s.build_proposal(CH_LOCAL, domains=["legal"], sensitivity="public").leads == []  # a lead for another domain is not shown


def test_a_domain_is_a_short_tag_never_the_task_text():
    s = Svc([local_world()])
    assert s.recommend(CH_LOCAL, text="t", description="x" * 80, domain=None, sensitivity="public")["ok"] is False and s.coord.harness.asked == []
    assert R.normalize_domain(None, "Contract Review") == ["contract review"] and R.normalize_domain(["a;b"], None) is None


def test_the_hash_changes_with_the_facts_and_not_without_them():
    s = Svc([local_world()], pol=policy(automated_use_cleared={"claude-code"}))
    a, b = s.build_proposal(CH_LOCAL, domains=["x"], sensitivity="public"), s.build_proposal(CH_LOCAL, domains=["x"], sensitivity="public")
    assert a.hash == b.hash
    s.facts._m[0].installed.discard("opencode")
    assert s.build_proposal(CH_LOCAL, domains=["x"], sensitivity="public").hash != a.hash


def test_an_entry_the_harness_cannot_install_says_so_with_a_typed_state_instead_of_pretending(tmp_path):
    m = machine(installed={"claude-code"}, signed_in={"claude-code:plan"})  # the local server runs; OpenCode is not on this machine
    s = Svc([m], pol=policy(automated_use_cleared={"claude-code"}))
    p = s.build_proposal(CH_LOCAL, domains=["x"], sensitivity="public")
    row = next(r for r in p.setup if r[0] == "opencode")
    assert "not installed" in row[2] and "mas_install_tool" in row[2] and "no_installer" not in row[2]  # its install data verifies by code: the proposal says the harness can install it, after a plan
    assert "opencode" not in {o.env for o in p.options}
    row_pi = next(r for r in p.setup if r[0] == "pi")
    assert "no_installer" in row_pi[2] and "mas_install_tool" not in row_pi[2]  # an entry with no install data says so with its typed state instead of pretending


def test_the_clearance_is_the_profiles_own_list_recorded_undoable_and_read_by_the_policy(tmp_path):
    seen = []
    p = Profile(tmp_path / "p.json", log=lambda t, d: seen.append(d))
    pol = policy(automated_use_cleared=set())
    assert p.clear_automated_use("codex") is True and p.clear_automated_use("codex") is False
    p.apply(pol)
    assert pol.automated_use_cleared == {"codex"} and Profile(tmp_path / "p.json").cleared == ["codex"] and seen[0]["new"] == "cleared"
    p.clear_automated_use("codex", False)
    p.apply(pol)
    assert pol.automated_use_cleared == set() and p.summary()["automated_use_cleared"] == []


# ---- the real coordinator, node and MCP server ----------------------------------------------------------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def pending(c, base):
    return [i for log in c.harness._logs.values() for i in log if i.kind == "ask_choice" and i.extra.get("state") == "pending" and i.extra.get("on_choice") and str(i.extra.get("base", "")).startswith(base)]


def test_a_request_no_type_names_gets_a_recommendation_and_one_question_not_a_dead_end(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_create_task", text="Draft the aims section of a grant proposal about soil carbon.", task_type="grant writing", touches=["none"])
            assert sc(r)["ok"] is True and sc(r)["question"] is True and "Recommendation for" in text(r) and "no admission suite exists" in text(r) and not c.services.tasks
            (q,) = pending(c, "recommend:")
            assert [o["id"] for o in q.payload["options"]][-1] == "neither" and "soil carbon" not in q.payload["text"]
            assert sc(r)["plan_hash"] in q.payload["text"]
            c.harness.decide(CH_CLOUD, q.id, "use_default")
            assert len(c.services.tasks) == 1
            (tid, st), = c.services.tasks.items()
            assert st["task"].task_type == "text" and st["task"].domain == ["grant writing"] and st["approved_route"].split("|")[0] in E.load_environments()
            assert [e.payload["option"] for e in c.log.events("recommendation_answered")] == ["use_default"]
            assert c.services.jobs[tid].state == "awaiting_approval"  # the owner approves the exact route afterwards, as for any task
        finally:
            await teardown()
    run(go())


def test_a_known_type_still_goes_straight_to_a_route_and_a_domain_tag_rides_along(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_create_task", text="Summarize: the sky is blue.", task_type="summarize", touches=["none"], domain=["education"])
            assert sc(r)["ok"] is True and "Recommendation for" not in text(r) and not pending(c, "recommend:")
            (st,) = c.services.tasks.values()
            assert st["task"].domain == ["education"] and st["task"].task_type == "summarize"
        finally:
            await teardown()
    run(go())


def test_a_confidential_request_from_a_local_chat_is_recommended_only_local_things_or_nothing(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.local.tool("mas_create_task", text="Review this contract for the Acme merger.", task_type="contract review", touches=["finance"])
            body = text(r)
            assert sc(r)["ok"] is False and "confidential" in body and "Nothing can be proposed" in body  # nothing is measured on this computer, so a confidential task waits: it is not guessed and never sent to a cloud access
            assert "meta-model-api" not in body.split("Nothing can be proposed")[0] and not pending(c, "recommend:") and not c.services.tasks
        finally:
            await teardown()
    run(go())


def test_the_clearance_question_is_asked_and_undone_through_the_profile_tool(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_profile", action="clear", id="claude-code")
            assert sc(r)["ok"] is True and "vendor's terms" in text(r) and "claude-code" not in c.services.policy.automated_use_cleared
            (q,) = pending(c, "profile:clear")
            c.harness.decide(CH_CLOUD, q.id, "clear")
            assert "claude-code" in c.services.policy.automated_use_cleared and [e.payload["id"] for e in c.log.events("automated_use_cleared")] == ["claude-code"]
            await host.tool("mas_profile", action="unclear", id="claude-code")
            assert "claude-code" not in c.services.policy.automated_use_cleared
            bad = await host.tool("mas_profile", action="clear", id="not-an-environment")
            assert sc(bad)["ok"] is False
        finally:
            await teardown()
    run(go())
