"""Router API additions of 2026-10-31 (docs/ROUTER_API.md sections 10-12): exclude, result_of, grade, result-of inputs with level inheritance, owner-approved decompositions, the ROUTER tag. Each property is a check
that drives the real router and also runs on a router with its guard removed, which must fail. SIMULATED-LOCAL: stand-ins, real TLS on loopback where the coordinator is involved."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "conformance"))  # routerrules lives with the conformance rules; this file must run alone too
import routerrules as RR  # noqa: E402

from envrec.contracts.enums import Actor
from envrec.contracts.models import Event
from envrec.contracts.tags import TAGS, derive_tag
from envrec_coordinator.router import engine, rule
from envrec_coordinator.router import Exclude, MachineFacts, Refusal, RouteDecision
from envrec_coordinator.router.model import Step
from envrec_net import envrun as E
from mas_world import coordinator, events
from test_environment_run import ENVS, build, finished, node_up, task
from test_mas_tasks import agent_up, down
from test_router import admission, machine, policy, router
from test_router import task as ptask


def run(coro):
    return asyncio.run(coro)


# ---- 1. exclude ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_an_excluded_machine_or_environment_is_never_chosen_at_any_step_and_the_exclusion_is_in_the_trace():
    machines = [machine("a"), machine("b")]
    r, _ = router(machines=machines, adm=[admission("code", "llamacpp-local", "a"), admission("code", "llamacpp-local", "b", passed=8)])
    free = r.route(ptask("t0"))
    assert free.route.machine_id == "a"  # without the exclusion machine a wins (9/10 against 8/10)
    r, _ = router(machines=machines, adm=[admission("code", "llamacpp-local", "a"), admission("code", "llamacpp-local", "b", passed=8)])
    d = r.route(ptask("t1").model_copy(update={"exclude": Exclude(machine_ids=["a"])}))
    assert d.route.machine_id == "b" and [s.step for s in d.rule_trace][:2] == ["sensitivity", "exclusion"]
    step = d.rule_trace[1]
    assert set(step.values.values()) == {"excluded_machine"} and step.margin is None and "never relaxed" in step.note
    r, _ = router(machines=machines)
    d = r.route(ptask("t2").model_copy(update={"exclude": Exclude(environment_ids=["llamacpp-local", "opencode"])}))
    assert d.route.environment_id not in ("llamacpp-local", "opencode")


def test_when_the_exclusion_leaves_nothing_the_refusal_is_the_normal_one_and_it_is_never_relaxed():
    r, _ = router(machines=[machine("a")])
    out = r.route(ptask("t1").model_copy(update={"exclude": Exclude(machine_ids=["a"])}))
    assert isinstance(out, Refusal) and out.code == "no_candidate" and out.details["excluded"]
    m = machine("a", model_endpoint="127.0.0.1:8080")
    r, _ = router(machines=[m], adm=[admission("text", "llamacpp-local", "a")])
    out = r.route(ptask("t2", "text", "confidential", "local").model_copy(update={"exclude": Exclude(machine_ids=["a"])}))
    assert out.code == "no_local_candidate"  # sensitivity still wins: the providers on machine a were never an option for confidential, and nothing else is left
    r, _ = router(machines=[m])
    assert r.route(ptask("t3", "text", "confidential", "local").model_copy(update={"exclude": Exclude(machine_ids=["zzz"])})).code == "provisional_only"


def test_the_exclusion_travels_with_the_task_so_every_escalation_honors_it():
    machines = [machine("a"), machine("b")]
    r, _ = router(machines=machines)
    t = ptask("t1").model_copy(update={"exclude": Exclude(machine_ids=["a"], environment_ids=["claude-code"])})
    d = r.route(t)
    seen = [d.route]
    for _ in range(2):
        d = r.report_result("t1", d.escalation_step, {"passed": False, "grader": "code", "evidence_ref": "g"})
        assert isinstance(d, RouteDecision)
        seen.append(d.route)
    assert all(x.machine_id == "b" and x.environment_id != "claude-code" for x in seen)
    assert r.report_result("t1", 2, {"passed": False, "grader": "code", "evidence_ref": "g"}).code == "escalation_exhausted"


def test_X1_holds_for_the_real_router_and_fails_on_one_without_the_exclusion_step(monkeypatch):
    assert RR.exclusion_violations() == []
    monkeypatch.setattr(engine, "exclusion_step", lambda cands, ex: (cands, {}, Step(step="exclusion", candidates_in=[], candidates_out=[])))
    assert any("despite exclude" in h or "still routed" in h for h in RR.exclusion_violations())


# ---- 5. result-of inputs and level inheritance --------------------------------------------------------------------------------------------------------------------------------------------

def test_a_result_inherits_the_level_of_its_source_so_a_verification_of_a_confidential_result_never_goes_to_a_provider():
    assert RR.inheritance_violations() == []
    machines = [machine("own")]
    adm = [admission("text", e, "own") for e in ("llamacpp-local", "claude-code", "codex")]
    r, _ = router(machines=machines, adm=adm)
    r.route(ptask("src", "text", "confidential", "local"))
    ver = ptask("ver", "text", "public", "local").model_copy(update={"payload_ref": "result-of:src"})
    d = r.route(ver)
    assert d.route.environment_id == "llamacpp-local" and d.effective_sensitivity == "confidential" and "raised from public by an input" in d.rule_trace[0].note
    other = ptask("two", "text", "internal", "local").model_copy(update={"payload_ref": "plain-ref", "input_refs": ["result-of:src"]})
    assert r.route(other).effective_sensitivity == "confidential"  # the highest among the task's own level and every input


def test_I1_fails_on_a_router_that_ignores_the_level_of_an_input(monkeypatch):
    monkeypatch.setattr(engine.Router, "_effective", lambda self, task: (task.sensitivity, []))
    hits = " ".join(RR.inheritance_violations())
    assert "routed to" in hits or "not confidential" in hits, hits


def test_an_input_of_unknown_source_is_treated_as_confidential_and_a_cloud_chat_cannot_ask_for_a_confidential_input():
    r, _ = router(adm=[admission("text", "llamacpp-local")])
    d = r.route(ptask("v", "text", "public", "local").model_copy(update={"payload_ref": "result-of:ghost"}))
    assert d.effective_sensitivity == "confidential" and "unknown source" in d.rule_trace[0].note and d.route.environment_id == "llamacpp-local"
    assert r.route(ptask("src", "text", "confidential", "local")).effective_sensitivity == "confidential"
    out = r.route(ptask("v2", "text", "public", "cloud").model_copy(update={"payload_ref": "result-of:src"}))
    assert isinstance(out, Refusal) and out.code == "confidential_from_cloud_chat"
    out = r.route(ptask("v3", "text", "public", "cloud").model_copy(update={"payload_ref": "result-of:ghost"}))
    assert out.code == "confidential_from_cloud_chat"  # fail closed: an unknown source counts as confidential here too


# ---- 2 + 5 end to end: result_of(handle) and dispatch of a result-of input (real TLS on loopback) ------------------------------------------------------------------------------------

def test_result_of_returns_the_state_and_a_capture_ref_and_a_verification_receives_the_output_under_the_sources_level(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                (tmp_path / "log").mkdir(exist_ok=True)
                node_up(agent, tmp_path, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "SECRET SUMMARY"}}]})))
                m = MachineFacts(machine_id=nid, os_family="darwin", installed={"claude-code"}, signed_in={"claude-code:plan"}, model_endpoint="127.0.0.1:8080", model_id="local-8b")
                adm = [(f"admission.text.{nid}.llamacpp-local.local-8b.local-hardware", {"status": "tested", "graded_by": "code", "passed": 9, "of": 10, "identity": {}})]
                r, log, _ = build(c, nid, machine=m, adm=adm)
                c.own_task("src")
                h = r.dispatch(r.route(task("src", "text", "confidential", "local")))
                early = r.result_of(h)
                assert (early["output_ref"] is None) == (early["state"] != "done")  # a reference exists exactly when the task is done
                await finished(c.tasks.tasks[h.node_task_id])
                res = r.result_of(h)
                assert res["state"] == "done" and len(res["output_ref"]) == 64 and r.outputs.get(res["output_ref"]).sensitivity == "confidential" and r.outputs.get(res["output_ref"]).text == "SECRET SUMMARY"
                # a confidential result cannot be handed to a cloud verifier, even if the decision was made under a lower level
                ver = task("ver", "text", "public", "local").model_copy(update={"payload_ref": "result-of:src"})
                d = r.route(ver)
                assert d.route.environment_id == "llamacpp-local"  # the provider was never a candidate for it
                forged = d.model_copy(update={"route": d.route.model_copy(update={"environment_id": "claude-code", "subtype": "provider", "model_id": "default", "access_id": "claude-code:plan"}), "effective_sensitivity": "public"})
                r._state["ver"]["decision"] = forged
                bad = r.dispatch(forged)
                assert isinstance(bad, Refusal) and bad.code == "confidential_cloud_environment"  # the stored level of the input wins at dispatch
                assert not [e for e in events(c, "task_created") if e.payload.get("routed_task") == "ver"]
                # the local verifier receives the output text as its input
                r._state["ver"]["decision"] = d
                c.own_task("ver")
                rec = await finished(c.tasks.tasks[r.dispatch(d).node_task_id])
                assert rec.status == "done"
            finally:
                await down(agent, tsk)
    run(go())


def test_a_public_result_can_be_verified_by_a_provider_and_the_text_reaches_it(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                (tmp_path / "log").mkdir(exist_ok=True)
                node_up(agent, tmp_path, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "public draft"}}]})))
                m = MachineFacts(machine_id=nid, os_family="darwin", installed={"claude-code"}, signed_in={"claude-code:plan"}, model_endpoint="127.0.0.1:8080", model_id="local-8b")
                r, _, _ = build(c, nid, machine=m)
                c.own_task("p1")
                h = r.dispatch(r.route(task("p1", "text", "public", "cloud")))
                await finished(c.tasks.tasks[h.node_task_id])
                assert r.result_of(h)["state"] == "done"
                ver = task("pv", "text", "public", "cloud").model_copy(update={"payload_ref": "result-of:p1", "exclude": Exclude(environment_ids=["llamacpp-local"])})
                d = r.route(ver)
                assert d.route.environment_id == "claude-code" and d.effective_sensitivity == "public"
                c.own_task("pv")
                rec = await finished(c.tasks.tasks[r.dispatch(d).node_task_id])
                import envstandins as S
                assert rec.status == "done" and S.calls(tmp_path / "log", "claude")[0]["stdin"] == "public draft"
            finally:
                await down(agent, tsk)
    run(go())


# ---- 3. grade --------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def _graded(spec, text="print(1)", **kw):
    r, log = router(**kw)
    ref = r.outputs.put("g", "public", {"output_text": text})
    r.set_grader("g", spec)
    return r, ref, log


@pytest.mark.parametrize("spec,text,passed", [({"kind": "equals", "value": "42"}, " 42\n", True), ({"kind": "equals", "value": "42"}, "43", False), ({"kind": "contains", "value": "ok"}, "all ok", True),
                                              ({"kind": "regex", "value": r"^def \w+\("}, "def f(x):\n  pass", True), ({"kind": "regex", "value": r"^def \w+\("}, "class A: pass", False)])
def test_a_text_code_grader_runs_in_the_coordinator_and_its_verdict_feeds_report_result(spec, text, passed):
    r, ref, log = _graded(spec, text)
    res = r.grade("g", ref)
    assert (res.passed, res.grader) == (passed, "code") and len(res.evidence_ref) == 64 and r.grade("g", ref).evidence_ref == res.evidence_ref  # same inputs, same evidence
    assert ("grade", "GUARD") in [(t, a) for t, a, _ in log]
    d = r.route(ptask("g"))
    after = r.report_result("g", 0, res.model_dump())
    assert (after is None) == passed


def test_a_grader_that_executes_model_written_code_runs_only_inside_our_sandbox_otherwise_it_is_refused():
    assert RR.grading_violations() == []
    seen = []
    r, ref, _ = _graded({"kind": "run_tests", "value": "assert f() == 1"})
    r.sandbox_runner = lambda rec, spec: (seen.append(rec.text) or {"passed": True, "log_sha256": "a" * 64})
    out = r.grade("g", ref)
    assert isinstance(out, Refusal) and out.code == "sandbox_missing" and seen == []
    r.sandbox_ready = lambda: True
    ok = r.grade("g", ref)
    assert ok.passed is True and ok.grader == "code" and seen == ["print(1)"]
    r.sandbox_runner = lambda rec, spec: {"passed": False, "log_sha256": "b" * 64}
    assert r.grade("g", ref).passed is False


def test_G1_fails_on_a_router_that_runs_the_runner_without_a_sandbox_and_passes_a_model_graded_result(monkeypatch):
    from envrec_coordinator.router import GradeResult

    def reckless(self, task_id, output_ref):
        self.sandbox_runner(self.outputs.get(output_ref), self._graders[task_id])  # executes first, asks never
        return GradeResult(passed=True, grader="code", evidence_ref="0" * 64)
    monkeypatch.setattr(engine.Router, "grade", reckless)
    hits = " ".join(RR.grading_violations())
    assert "model-written code was handed to the runner with no sandbox ready" in hits and "a model-graded result was not refused as weak" in hits and "a human-graded result was not refused as weak" in hits, hits


def test_model_and_human_graded_results_never_pass_and_a_missing_grader_or_output_is_refused():
    for kind in ("model", "human"):
        r, ref, _ = _graded({"kind": kind, "value": ""})
        assert r.grade("g", ref).code == "weak_grader"
    r, ref, _ = _graded({"kind": "equals", "value": "x"})
    assert r.grade("g", "f" * 64).code == "output_missing" and r.grade("other", ref).code == "no_grader"
    other = r.outputs.put("other-task", "public", {"output_text": "x"})
    assert r.grade("g", other).code == "output_missing"  # an output of another task is not this task's
    r.set_grader("g", {"kind": "banana", "value": "x"})
    assert r.grade("g", ref).code == "no_grader"
    r.set_grader("g", {"kind": "regex", "value": "("})
    assert r.grade("g", ref).code == "no_grader"  # a pattern that does not compile is refused, not passed


# ---- 6. owner-approved decomposition: the authority is the recorded OWNER event, subtasks are matched by id ----------------------------------------------------------------------------

SUBS = [{"id": "s1", "type": "code", "sensitivity": "public"}, {"id": "s2", "type": "code", "sensitivity": "public"}, {"id": "s3", "type": "text", "sensitivity": "confidential"}]


def decided(c, subs=SUBS, parent="parent", gesture="os_dialog", by="owner"):
    return c.record_owner_decision(parent, subs, gesture=gesture, by=by)


def test_approve_decomposition_returns_the_decomposition_id_when_the_store_holds_an_owner_decision_for_exactly_that_decomposition(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            ev = decided(c)
            did = c.approve_decomposition("parent", SUBS, ev)
            assert isinstance(did, str) and did.startswith("dec_") and c.approve_decomposition("parent", SUBS, ev) == did  # the same decision gives the same id, once
            assert len([e for e in c.log.events("decomposition_approved")]) == 1
            assert c.approve_decomposition("parent", SUBS, decided(c)) != did  # another decision, another decomposition
            for gesture in ("os_dialog", "host_dialog", "panel"):
                assert isinstance(c.approve_decomposition("parent", SUBS, decided(c, gesture=gesture)), str), gesture
            import inspect
            assert "by" not in inspect.signature(c.approve_decomposition).parameters  # the caller cannot vouch for itself: authority comes from the recorded event
    run(go())


def test_approve_decomposition_refuses_with_no_owner_origin_unless_the_recorded_event_is_the_owners_human_gesture_for_this_exact_decomposition(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            good = decided(c)
            cases = {
                "unknown event": ("parent", SUBS, "evt_made_up"),
                "the operator's decision": ("parent", SUBS, decided(c, by="claude-code")),
                "no human gesture": ("parent", SUBS, decided(c, gesture="script")),
                "a decision by code, not the owner": ("parent", SUBS, c.log.add("decomposition_decision", {"parent": "parent", "subtasks": SUBS, "gesture": "panel"}, owner=False).id),
                "another parent": ("other", SUBS, good),
                "a changed subtask": ("parent", [{**SUBS[0], "type": "text"}, *SUBS[1:]], good),
                "a changed level": ("parent", [{**SUBS[0], "sensitivity": "internal"}, *SUBS[1:]], good),
                "an extra subtask": ("parent", [*SUBS, {"id": "s4", "type": "code", "sensitivity": "public"}], good),
                "a missing subtask": ("parent", SUBS[:2], good),
                "reordered": ("parent", [SUBS[1], SUBS[0], SUBS[2]], good),
                "another kind of event": ("parent", SUBS, c.own_task("parent")),
                "duplicate ids": ("parent", [SUBS[0], SUBS[0]], decided(c, subs=[SUBS[0], SUBS[0]])),
            }
            for name, (parent, subs, ev) in cases.items():
                out = c.approve_decomposition(parent, subs, ev)
                assert isinstance(out, Refusal) and out.code == "no_owner_origin" and "created or approved" in out.reason, name
            assert c.log.events("decomposition_approved") == []  # nothing was recorded for any refusal
    run(go())


def test_own_subtask_matches_by_the_subtask_id_on_the_owners_list_and_takes_type_and_level_from_that_list(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            ev = decided(c)
            did = c.approve_decomposition("parent", SUBS, ev)
            import inspect
            assert list(inspect.signature(c.own_subtask).parameters) == ["task_id", "decomposition_id", "subtask_id"]  # no type or level from the caller, no order matching
            assert c.own_subtask("t-a", did, "s2") == ev and c._owner_origin("t-a") == ev  # the origin IS the owner's decision event
            assert c.own_subtask("t-b", did, "s1") == ev and c.own_subtask("t-c", did, "s3") == ev  # order of registration is irrelevant
            assert c.own_subtask("t-x", did, "s2") is None and c._owner_origin("t-x") is None  # s2 is already held by t-a
            assert c.own_subtask("t-y", did, "s9") is None  # not on the list
            assert c.own_subtask("t-z", "dec_made_up", "s1") is None
            assert c._owner_origin("t-a") == ev  # earlier ones are unaffected
            assert c.own_subtask("t-a", did, "s2") == ev  # the holder may ask again
    run(go())


def test_the_task_actually_sent_must_be_of_the_listed_type_and_at_least_the_listed_level(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            ev = decided(c)
            did = c.approve_decomposition("parent", SUBS, ev)
            c.own_subtask("a", did, "s1")  # code / public
            c.own_subtask("b", did, "s3")  # text / confidential
            assert c._owner_origin("a", "code", "public") == ev and c._owner_origin("a", "code", "confidential") == ev  # a stricter level is fine
            assert c._owner_origin("a", "text", "public") is None  # another type
            assert c._owner_origin("b", "text", "confidential") == ev
            assert c._owner_origin("b", "text", "public") is None and c._owner_origin("b", "text", "internal") is None  # a lower level than the owner listed would be more permissive than approved
    run(go())


def test_the_owner_decision_is_checked_again_from_the_store_so_a_record_written_by_code_gives_no_origin(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            op = decided(c, by="claude-code")
            c.log.add("decomposition_approved", {"id": "dec_forged", "parent": "parent", "subtasks": SUBS, "count": 3, "owner_event": op, "gesture": "os_dialog"})
            c.log.add("environment_task_origin", {"task": "f", "kind": "subtask", "decomposition": "dec_forged", "subtask_id": "s1", "task_type": "code", "sensitivity": "public"})
            assert c._owner_origin("f") is None  # the record exists but the decision behind it is the operator's
            nodec = c.log.add("decomposition_approved", {"id": "dec_ghost", "parent": "parent", "subtasks": SUBS, "count": 3, "owner_event": "evt_none", "gesture": "panel"}).id
            c.log.add("environment_task_origin", {"task": "g", "kind": "subtask", "decomposition": "dec_ghost", "subtask_id": "s1", "task_type": "code", "sensitivity": "public"})
            assert nodec and c._owner_origin("g") is None
    run(go())


def test_a_dispatched_subtask_leaves_only_with_the_owners_decision_and_one_outside_the_list_is_refused(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                (tmp_path / "log").mkdir(exist_ok=True)
                node_up(agent, tmp_path)
                m = MachineFacts(machine_id=nid, os_family="darwin", installed={"claude-code"}, signed_in={"claude-code:plan"})
                r, _, _ = build(c, nid, machine=m)
                subs = [{"id": "only", "type": "code", "sensitivity": "public"}]
                did = c.approve_decomposition("parent", subs, decided(c, subs=subs))
                c.own_subtask("sub1", did, "only")
                c.own_subtask("sub2", did, "only")  # a second task for the one listed subtask
                ok = r.dispatch(r.route(task("sub1").model_copy(update={"created_by": "PLANNER_ACCEPTED"})))
                assert (await finished(c.tasks.tasks[ok.node_task_id])).status == "done"
                bad = r.dispatch(r.route(task("sub2").model_copy(update={"created_by": "PLANNER_ACCEPTED"})))
                assert isinstance(bad, Refusal) and bad.code == "no_owner_origin"
                c.own_subtask("sub3", did, "only")
                wrong_type = r.dispatch(r.route(task("sub3", "text").model_copy(update={"created_by": "PLANNER_ACCEPTED"})))
                assert isinstance(wrong_type, Refusal) and wrong_type.code == "no_owner_origin"  # not even registered: the id is held by sub1
            finally:
                await down(agent, tsk)
    run(go())


# ---- 4. verifier instructions as a stored reference ------------------------------------------------------------------------------------------------------------------------------------

def test_instr_verify_is_a_stored_reference_that_never_raises_the_level_and_unknown_ones_are_refused(tmp_path):
    from envrec_coordinator.router import Router
    assert E.load_instructions()["instr:verify"].startswith("You are verifying the output of another task")
    r, _ = router(adm=[admission("text", "llamacpp-local")])
    d = r.route(ptask("iv", "text", "public", "local").model_copy(update={"payload_ref": "instr:verify"}))
    assert d.effective_sensitivity == "public"  # project-written text with no task data
    r.route(ptask("srcc", "text", "confidential", "local"))
    d2 = r.route(ptask("iv2", "text", "public", "local").model_copy(update={"payload_ref": "instr:verify", "input_refs": ["result-of:srcc"]}))
    assert d2.effective_sensitivity == "confidential"  # the instruction does not hide the confidential input
    r2 = Router(envs=ENVS, facts=r.facts, policy=r.policy, instructions={"instr:verify": "custom"}, sender=object(), payloads=lambda ref: "x")
    assert r2.instructions == {"instr:verify": "custom"}


def test_a_verifier_receives_the_stored_instructions_then_the_output_it_verifies(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, tsk, nid = await agent_up(c, tmp_path)
            try:
                (tmp_path / "log").mkdir(exist_ok=True)
                node_up(agent, tmp_path, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "public draft"}}]})))
                m = MachineFacts(machine_id=nid, os_family="darwin", installed={"claude-code"}, signed_in={"claude-code:plan"}, model_endpoint="127.0.0.1:8080", model_id="local-8b")
                r, _, _ = build(c, nid, machine=m)
                c.own_task("p1")
                h = r.dispatch(r.route(task("p1", "text", "public", "cloud")))
                await finished(c.tasks.tasks[h.node_task_id])
                r.result_of(h)
                ver = task("pv", "text", "public", "cloud").model_copy(update={"payload_ref": "instr:verify", "input_refs": ["result-of:p1"], "exclude": Exclude(environment_ids=["llamacpp-local"])})
                d = r.route(ver)
                c.own_task("pv")
                rec = await finished(c.tasks.tasks[r.dispatch(d).node_task_id])
                import envstandins as S
                sent = S.calls(tmp_path / "log", "claude")[-1]["stdin"]
                assert rec.status == "done" and sent == r.instructions["instr:verify"] + "\n\n--- input 2 ---\n\npublic draft"
                missing = task("pm", "text", "public", "cloud").model_copy(update={"payload_ref": "instr:nope"})
                c.own_task("pm")
                out = r.dispatch(r.route(missing))
                assert isinstance(out, Refusal) and out.code == "payload_missing"
            finally:
                await down(agent, tsk)
    run(go())


# ---- 7. the ROUTER tag ---------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_route_decision_and_an_escalation_are_tagged_router_and_a_refusal_stays_guard(tmp_path):
    assert "ROUTER" in TAGS
    assert derive_tag(Actor.loop, "route_decision") == "ROUTER" and derive_tag(Actor.loop, "escalation") == "ROUTER"
    assert derive_tag(Actor.loop, "route_refusal") == "GUARD" and derive_tag(Actor.loop, "dispatch") == "GUARD" and derive_tag(Actor.human, "route_decision") == "OWNER"
    assert Event(actor=Actor.loop, type="x", payload={}, tag="ROUTER").effective_tag == "ROUTER"
    async def go():
        async with coordinator(tmp_path / "c") as c:
            c.log.add("route_decision", {"task_id": "t"})
            c.log.add("escalation", {"task_id": "t"})
            c.log.add("route_refusal", {"task_id": "t"})
            assert [e.effective_tag for e in c.log.events()][-3:] == ["ROUTER", "ROUTER", "GUARD"]
            assert [e.tag for e in c.log.events("route_decision")] == ["ROUTER"]  # stored, not only derived
    run(go())
