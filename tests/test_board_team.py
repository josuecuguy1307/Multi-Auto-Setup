"""The director/workers loop against the router API (docs/ROUTER_API.md), with the SIMULATED-LOCAL stand-ins of tests/board_sim.py."""
import pytest

from board_sim import LoopbackModelServer, StandInRouter
from envrec_board.board import Board
from envrec_board.decisions import DecisionDesk
from envrec_board.intents import IntentEmitter, Sink
from envrec_board.log import MemoryLog
from envrec_board.team import TeamPolicy, TeamRun, render_view
from envrec_board.vocab import BoardError

POST_A = {"action": "post_subtask", "args": {"task_type": "text", "input_ref": "doc-1", "expected": "a summary", "test_ref": "contains:summary-A"}}
POST_B = {"action": "post_subtask", "args": {"task_type": "code", "input_ref": "doc-2", "expected": "an add function", "test_ref": "contains:def add"}}
SUBMIT = {"action": "submit_plan", "args": {}}
VERIFY = lambda tid: {"action": "request_verification", "args": {"subtask_id": tid}}  # noqa: E731
STOP = {"action": "escalate_to_owner", "args": {"question": "stop"}}
STORE = {"doc-1": "Summarize document one", "doc-2": "Write add(a, b) in Python", "instr:verify": "Verify the result below and answer VERIFIED or REJECTED."}


def answers(machine, prompt):
    if "answer VERIFIED or REJECTED" in prompt:
        return "VERIFIED: the result is correct"
    if "add(a, b)" in prompt:
        return "def add(a, b): return a + b" if machine == "mac" else "I cannot do that"
    return "summary-A: done"


class Script:
    def __init__(self, *actions):
        self.actions = list(actions)
        self.views = []

    def __call__(self, view):
        self.views.append(view)
        return self.actions.pop(0) if self.actions else STOP


class IgnoresExclude(StandInRouter):
    def route(self, task):
        return super().route({k: v for k, v in task.items() if k != "exclude"})


class Sys:
    def __init__(self, sensitivity="public", entry="local", admission=None, policy=None, sinks=None, filter_on=False, router_cls=StandInRouter, **router_kw):
        self.server = LoopbackModelServer(answers)
        self.board = Board(MemoryLog(), lambda: 1000.0)
        self.router = router_cls(self.server.port, dict(STORE), admission=admission, **router_kw)
        self.desk = DecisionDesk(self.board)
        self.out = []
        self.em = IntentEmitter(self.board, self.desk, [Sink("panel", "local", self.out.append)] + (sinks or []), outbound_filter=self.router.filter_outbound if filter_on else None)
        self.root = self.board.post_task("goal", "Prepare my notes", "done", "owner", sensitivity, "owner", actor="OWNER")
        self.chat = {"kind": entry, "name": "owner-panel" if entry == "local" else "claude-desktop"}
        self.team = TeamRun(self.board, self.desk, self.router, root_id=self.root, entry_chat=self.chat, policy=policy)

    def owner(self, kind):
        """The owner's gesture (system dialog) on the oldest pending decision of this kind."""
        d = next(d for d in self.desk.pending() if d["kind"] == kind)
        self.desk.answer(d["decision_id"], "approve", channel="os_dialog", by="owner")
        return d

    def calls(self, name):
        return [c for c in self.router.calls if c[0] == name]

    def close(self):
        self.server.stop()


@pytest.fixture
def sy():
    made = []

    def make(**kw):
        s = Sys(**kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def planned(s, *script):
    """Run until the director's plan is waiting for the owner, approve it by gesture, run again. Returns the director script."""
    d = Script(*script)
    assert s.team.run(d)["state"] == "waiting_for_owner"
    s.owner("decompose")
    return d


# ---- the owner's plan -------------------------------------------------------------------------------------------------------------------------

def test_nothing_is_registered_or_routed_before_the_owner_approves_the_plan(sy):
    s = sy()
    d = Script(POST_A, POST_B, SUBMIT)
    r = s.team.run(d)
    assert r["state"] == "waiting_for_owner"
    assert not [c for c in s.router.calls if c[0] in ("route", "own_subtask", "approve_decomposition", "dispatch")]
    assert [p["kind"] for p in s.desk.pending()] == ["decompose"] and s.router.origin == {}


def test_the_plan_lists_each_subtask_and_one_verification_slot_each(sy):
    s = sy()
    planned(s, POST_A, POST_B, SUBMIT)
    ask = next(e for e in s.board.log.entries() if e.type == "board.decision_pending")
    items = ask.payload["details"]["items"]
    assert [(i["kind"], i["task_id"], i["for"], i["task_type"], i["sensitivity"]) for i in items] == [
        ("subtask", "task-2", None, "text", "public"), ("verification_slot", None, "task-2", "text", "public"),
        ("subtask", "task-3", None, "code", "public"), ("verification_slot", None, "task-3", "code", "public")]
    assert [i["input_ref"] for i in items if i["kind"] == "subtask"] == ["doc-1", "doc-2"] and "Only these are allowed" in ask.payload["summary"]
    assert s.calls("approve_decomposition") == [("approve_decomposition", s.root, 4)]


def test_the_routers_owner_event_is_written_only_from_the_owners_gesture(sy):
    s = sy()
    d = Script(POST_A, SUBMIT)
    s.team.run(d)
    p = s.desk.pending()[0]
    s.desk.answer(p["decision_id"], "approve", channel="panel", by="claude-code")  # the operator is not the owner
    assert s.calls("approve_decomposition") == []
    with pytest.raises(BoardError):
        s.desk.answer(p["decision_id"], "approve", channel="chat_tool_call", by="owner")
    assert s.calls("approve_decomposition") == []
    s.desk.answer(p["decision_id"], "approve", channel="host_dialog", by="owner")
    assert len(s.calls("approve_decomposition")) == 1


def test_an_answer_needs_an_explicit_by_so_a_bare_answer_never_counts(sy):
    s = sy()
    s.team.run(Script(POST_A, SUBMIT))
    with pytest.raises(TypeError):
        s.desk.answer(s.desk.pending()[0]["decision_id"], "approve", channel="os_dialog")


def test_a_rejected_plan_routes_nothing_and_the_director_can_drop_the_subtask(sy):
    s = sy()
    d = Script(POST_A, SUBMIT, {"action": "reject_result", "args": {"subtask_id": "task-2", "reason": "owner said no"}}, {"action": "finish", "args": {"summary": "nothing to do"}})
    s.team.run(d)
    s.desk.answer(s.desk.pending()[0]["decision_id"], "reject", channel="os_dialog", by="owner")
    r = s.team.run(d)
    assert r == {"state": "stopped", "stop": "finished"} and not s.calls("route") and not s.calls("own_subtask")


def test_a_subtask_posted_after_the_approval_waits_for_a_new_plan(sy):
    s = sy()
    d = planned(s, POST_A, SUBMIT, POST_B, SUBMIT)
    s.team.run(d)  # A runs and passes; the director posts B, which is not on the approved list
    assert s.board.tasks["task-2"]["status"] == "done" and s.board.tasks["task-3"]["route"] is None
    assert [c[1] for c in s.calls("own_subtask")] == ["task-2"] and [p["kind"] for p in s.desk.pending()] == ["decompose"]  # B is not listed; the director asked again
    s.owner("decompose")
    s.team.run(d)
    assert s.board.tasks["task-3"]["route"] is not None


# ---- verification slots and exclusion ------------------------------------------------------------------------------------------------------

def test_a_verification_uses_its_slot_without_asking_again_and_carries_exclude(sy):
    s = sy()
    d = planned(s, POST_A, SUBMIT, VERIFY("task-2"), STOP)
    s.team.run(d)
    v = s.board.tasks["task-3"]
    assert v["verifies"] == "task-2" and v["exclude"] == {"machine_ids": ["lenovo"], "environment_ids": ["llamacpp"]}
    assert s.router.tasks["task-3"]["exclude"] == v["exclude"] and s.board.machine_of("task-3") == "mac" and v["status"] == "done"
    assert not [p for p in s.desk.pending() if p["kind"] == "decompose"]  # the slot was pre-approved
    assert v["worked_by"] == "mac" != s.board.tasks["task-2"]["worked_by"]


def test_an_extra_verification_beyond_the_slot_needs_a_new_owner_approval(sy):
    s = sy()
    planned(s, POST_A, SUBMIT)
    s.team.pump()  # A runs on lenovo
    s.team.director_turn(lambda view: VERIFY("task-2"))
    s.team.pump()
    assert s.board.tasks["task-3"]["status"] == "done"  # the pre-approved slot
    s.board.post_task("verification", "result-of:task-2", "e", "t", "public", "director", actor="MODEL", parent=s.root, verifies="task-2", input_refs=["instr:verify"],
                      exclude={"machine_ids": ["lenovo"]})
    routes_before = len(s.calls("route"))
    s.team.pump()
    assert s.board.tasks["task-4"]["route"] is None and len(s.calls("route")) == routes_before
    plan = s.team.submit_plan()
    items = s.desk.pending()[0]["details"]["items"]
    assert [(i["kind"], i["task_id"]) for i in items] == [("verification", "task-4")] and plan["kind"] == "decompose"
    s.owner("decompose")
    s.team.pump()
    assert s.board.tasks["task-4"]["route"] is not None


def test_a_verification_cannot_be_posted_with_a_lower_sensitivity_than_its_subtask(sy):
    s = sy(sensitivity="internal", open_providers_for=("internal",))
    planned(s, POST_A, SUBMIT)
    with pytest.raises(BoardError) as r:
        s.board.post_task("verification", "result-of:task-2", "e", "t", "public", "director", actor="MODEL", parent=s.root, verifies="task-2")
    assert r.value.code == "sensitivity_lowered"


def test_when_exclusion_leaves_no_candidate_the_routers_normal_refusal_is_recorded(sy):
    s = sy(open_providers_for=())  # no provider is open, so lenovo is the only candidate and the exclusion removes it
    d = planned(s, POST_A, SUBMIT, VERIFY("task-2"), STOP)
    s.team.run(d)
    v = s.board.tasks["task-3"]
    assert v["route_refusal"]["code"] == "no_candidate" and v["lease_id"] is None
    assert [c for c in s.calls("dispatch") if c[1] == "task-3"] == []


def test_the_board_still_refuses_a_verifier_on_the_machine_that_did_the_work_if_the_router_ignores_exclude(sy):
    s = sy(router_cls=IgnoresExclude)
    d = planned(s, POST_A, SUBMIT, VERIFY("task-2"), STOP)
    s.team.run(d)
    v = s.board.tasks["task-3"]
    assert v["route_refusal"]["code"] == "verifier_not_independent" and [c for c in s.calls("dispatch") if c[1] == "task-3"] == []


def test_a_confidential_verification_whose_only_local_machine_is_excluded_is_refused(sy):
    s = sy(sensitivity="confidential", admission={"llamacpp@lenovo": 9})
    d = planned(s, POST_A, SUBMIT, VERIFY("task-2"), STOP)
    s.team.run(d)
    v = s.board.tasks["task-3"]
    assert s.router.tasks["task-3"]["effective"] == "confidential"  # a result inherits the level of its source
    assert v["route_refusal"]["code"] == "no_local_candidate" and [c for c in s.calls("dispatch") if c[1] == "task-3"] == []


# ---- grading through the router ----------------------------------------------------------------------------------------------------------------

def test_the_grader_is_set_by_the_creator_and_the_verdict_comes_from_grade(sy):
    s = sy()
    d = planned(s, POST_A, SUBMIT, STOP)
    s.team.run(d)
    assert ("set_grader", "task-2", "contains") in s.router.calls and s.calls("grade") == [("grade", "task-2")]
    t = s.board.tasks["task-2"]
    assert t["status"] == "done" and len(t["result"]["output"]) == 64 and t["result"]["evidence"] and "summary-A" not in str(t["result"])  # the board keeps references, not content


def test_a_task_with_no_code_grader_keeps_a_hypothesis_and_reports_nothing(sy):
    s = sy()
    d = planned(s, {"action": "post_subtask", "args": {"task_type": "text", "input_ref": "doc-1", "expected": "e", "test_ref": "looks-good"}}, SUBMIT, STOP)
    s.team.run(d)
    assert s.board.tasks["task-2"]["status"] == "unverified" and not s.calls("report_result") and not s.calls("set_grader")
    assert any("[no_grader]" in i["text_fallback"] and "hypothesis" in i["text_fallback"] for i in s.out)


def test_run_tests_without_a_sandbox_is_refused_and_the_result_stays_a_hypothesis(sy):
    s = sy()
    d = planned(s, {"action": "post_subtask", "args": {"task_type": "code", "input_ref": "doc-2", "expected": "e", "test_ref": "run_tests:suite-1"}}, SUBMIT, STOP)
    s.team.run(d)
    assert s.board.tasks["task-2"]["status"] == "unverified" and any("[sandbox_missing]" in i["text_fallback"] for i in s.out)


# ---- escalation ---------------------------------------------------------------------------------------------------------------------------------

def test_with_escalations_set_to_ask_a_failed_code_grade_escalates_only_after_the_owner_approves(sy):
    s = sy(policy=TeamPolicy(preapproved_escalations=0))
    d = planned(s, POST_B, SUBMIT, STOP)
    r = s.team.run(d)
    assert r["state"] == "waiting_for_owner" and s.board.tasks["task-2"]["status"] == "failed"
    esc = next(p for p in s.desk.pending() if p["kind"] == "escalate_beyond_preapproved")
    assert s.board.tasks["task-2"]["proposal"]["decision"]["escalation_step"] == 1 and "failed its code test" in esc["summary"]
    assert [c[2] for c in s.calls("dispatch")] == ["lenovo"]
    s.owner("escalate_beyond_preapproved")
    s.team.run(d)
    assert [c[2] for c in s.calls("dispatch")] == ["lenovo", "mac"] and s.board.tasks["task-2"]["status"] == "done"
    assert len(s.calls("approve_decomposition")) == 1 and len(s.calls("own_subtask")) == 1  # no second plan for the escalation


def test_a_declined_escalation_leaves_the_task_failed_and_runs_nothing_more(sy):
    s = sy(policy=TeamPolicy(preapproved_escalations=0))
    d = planned(s, POST_B, SUBMIT, STOP)
    s.team.run(d)
    e = next(p for p in s.desk.pending() if p["kind"] == "escalate_beyond_preapproved")
    s.desk.answer(e["decision_id"], "reject", channel="panel", by="owner")
    s.team.run(d)
    assert s.board.tasks["task-2"]["status"] == "failed" and s.board.tasks["task-2"]["proposal"] is None and len(s.calls("dispatch")) == 1
    assert any("declined the escalation" in i["text_fallback"] for i in s.out)


def test_by_default_an_escalation_within_a_task_runs_without_a_question_and_the_owner_is_told(sy):
    s = sy()
    d = planned(s, POST_B, SUBMIT, STOP)
    s.team.run(d)
    t = s.board.tasks["task-2"]
    assert t["status"] == "done" and t["escalation_step"] == 1 and [c[2] for c in s.calls("dispatch")] == ["lenovo", "mac"]
    assert not [p for p in s.desk.pending() if p["kind"] == "escalate_beyond_preapproved"] and len(s.calls("approve_decomposition")) == 1  # no question, no new plan
    notice = [i for i in s.out if i["kind"] == "progress" and i["payload"].get("state") == "escalated"]
    assert len(notice) == 1 and notice[0]["payload"]["escalation_step"] == 1 and "moved up to escalation step 1" in notice[0]["text_fallback"]


def test_the_third_escalation_is_refused_and_the_owner_is_told(sy):
    s = sy()  # the default allows both steps the router has
    d = planned(s, {"action": "post_subtask", "args": {"task_type": "code", "input_ref": "doc-2", "expected": "e", "test_ref": "contains:NEVER"}}, SUBMIT, STOP)
    s.team.run(d)
    t = s.board.tasks["task-2"]
    assert t["status"] == "failed" and t["route_refusal"]["code"] == "escalation_exhausted"
    assert any("[escalation_exhausted]" in i["text_fallback"] for i in s.out)


# ---- sensitivity ----------------------------------------------------------------------------------------------------------------------------------

def test_a_confidential_task_from_a_cloud_chat_stops_the_run_with_the_routers_reason(sy):
    s = sy(sensitivity="confidential", entry="cloud")
    d = planned(s, POST_A, SUBMIT, STOP)
    r = s.team.run(d)
    assert r == {"state": "stopped", "stop": "refused"} and not s.board.leases and not s.calls("dispatch")
    assert any("only be created from the owner panel" in i["text_fallback"] for i in s.out)


def test_confidential_without_admission_facts_is_provisional_only_and_never_runs(sy):
    s = sy(sensitivity="confidential")
    d = planned(s, POST_A, SUBMIT, STOP)
    s.team.run(d)
    assert s.board.tasks["task-2"]["route_refusal"]["code"] == "provisional_only" and not s.board.leases and not s.calls("dispatch")
    view = render_view(s.board, s.root, s.team.tools.view)
    assert "[provisional_only]" in view and "reject_result" in view


def test_confidential_with_a_measured_local_combination_runs_locally(sy):
    s = sy(sensitivity="confidential", admission={"llamacpp@lenovo": 9})
    d = planned(s, POST_A, SUBMIT, STOP)
    s.team.run(d)
    t = s.board.tasks["task-2"]
    assert t["status"] == "done" and s.board.machine_of("task-2") == "lenovo" and t["route"]["evidence_label"] == "measured here 9/10"


def test_the_director_view_has_no_route_candidates_or_trace(sy):
    s = sy()
    d = planned(s, POST_A, SUBMIT, STOP)
    s.team.run(d)
    view = render_view(s.board, s.root, s.team.tools.view)
    assert "rule_trace" not in view and "claude-code" not in view and "llamacpp" not in view and "lenovo" not in view and "exclude" not in view


def test_cloud_chat_gets_confidential_progress_only_through_the_routers_filter(sy):
    cloud = []
    s = sy(sensitivity="confidential", admission={"llamacpp@lenovo": 9}, sinks=[Sink("claude-desktop", "cloud", cloud.append)], filter_on=True)
    d = planned(s, POST_A, SUBMIT, STOP)
    s.team.run(d)
    assert cloud and all(i.get("redacted") for i in cloud if i["sensitivity"] == "confidential")
    assert "Summarize document one" not in str(cloud) and "summary-A" not in str(cloud) and "Prepare my notes" not in str(cloud)
    assert all(chat["kind"] == "cloud" for _, chat in s.router.filtered)


# ---- protocol -----------------------------------------------------------------------------------------------------------------------------------

def test_the_loopback_model_server_listens_on_loopback_only(sy):
    assert sy().server.httpd.server_address[0] == "127.0.0.1"


def test_no_progress_still_ends_the_protocol_when_the_director_repeats_itself(sy):
    s = sy()
    d = planned(s, POST_A, SUBMIT, *([POST_A] * 8))
    assert s.team.run(d) == {"state": "stopped", "stop": "no_progress"}


def test_the_whole_run_replays_to_the_same_state(sy):
    s = sy(policy=TeamPolicy(preapproved_escalations=0))
    d = planned(s, POST_B, SUBMIT, STOP)
    s.team.run(d)
    s.owner("escalate_beyond_preapproved")
    s.team.run(d)
    assert Board.replay(s.board.log.entries()).snapshot() == s.board.snapshot()


def test_events_name_who_decided(sy):
    s = sy(policy=TeamPolicy(preapproved_escalations=0))
    d = planned(s, POST_B, SUBMIT, STOP)
    s.team.run(d)
    s.owner("escalate_beyond_preapproved")
    s.team.run(d)
    tags = {}
    for e in s.board.log.entries():
        tags.setdefault(e.type, set()).add(e.tag)
    assert tags["board.route_decision"] == {"ROUTER"} and tags["board.decision_answered"] == {"OWNER"} and tags["board.task"] == {"OWNER", "MODEL"}
    assert tags["board.lease"] == {"GUARD"} and tags["board.escalation_proposed"] == {"ROUTER"} and "CLAUDE-CODE" not in {t for v in tags.values() for t in v}


# ---- the router's owner decision, rechecked by id (ROUTER_API section 12) --------------------------------------------------------------------

class RefusesPlan(StandInRouter):
    def approve_decomposition(self, parent_task_id, subtasks, owner_event_id):
        super().approve_decomposition(parent_task_id, subtasks, owner_event_id)
        return self._refuse(parent_task_id, "no_owner_origin")


class NoSlot(StandInRouter):
    def own_subtask(self, task_id, decomposition_id, subtask_id):
        super().own_subtask(task_id, decomposition_id, subtask_id)
        return None


def test_the_gesture_is_recorded_with_the_router_before_it_approves_and_only_for_the_owner(sy):
    s = sy()
    s.team.run(Script(POST_A, SUBMIT))
    p = s.desk.pending()[0]
    s.desk.answer(p["decision_id"], "approve", channel="panel", by="claude-code")
    assert s.calls("record_owner_decision") == []  # the operator's answer never reaches the router
    s.desk.answer(p["decision_id"], "approve", channel="host_dialog", by="owner")
    assert s.calls("record_owner_decision") == [("record_owner_decision", s.root, "host_dialog")]
    names = [c[0] for c in s.router.calls]
    assert names.index("record_owner_decision") < names.index("approve_decomposition")
    assert s.router.events["owner-ev-1"]["tag"] == "OWNER"


def test_subtasks_are_matched_by_their_explicit_ids_including_the_slot_id(sy):
    s = sy()
    d = planned(s, POST_A, SUBMIT, VERIFY("task-2"), STOP)
    s.team.run(d)
    ask = next(e for e in s.board.log.entries() if e.type == "board.decision_pending")
    assert [i["subtask_id"] for i in ask.payload["details"]["items"]] == ["task-2", "verify-of:task-2"]
    assert s.calls("own_subtask") == [("own_subtask", "task-2", "task-2"), ("own_subtask", "task-3", "verify-of:task-2")]
    assert s.router.origin["task-3"]["event"] == s.router.origin["task-2"]["event"] == "owner-ev-1"


def test_a_verification_goes_to_the_router_as_instructions_plus_the_result_under_check(sy):
    s = sy()
    d = planned(s, POST_A, SUBMIT, VERIFY("task-2"), STOP)
    s.team.run(d)
    v = s.board.tasks["task-3"]
    assert v["input"] == "instr:verify" and v["input_refs"] == ["result-of:task-2"]
    prompt = next(p for m, p in s.server.requests if "VERIFIED or REJECTED" in p)
    assert prompt.startswith("Verify the result below") and "--- input 2 ---\nsummary-A: done" in prompt


def test_a_plan_the_router_does_not_accept_registers_nothing_and_the_owner_is_told(sy):
    s = sy(router_cls=RefusesPlan)
    d = Script(POST_A, SUBMIT, STOP)
    s.team.run(d)
    s.owner("decompose")
    assert any(e.type == "board.plan_refused" for e in s.board.log.entries()) and not any(e.type == "board.plan_approved" for e in s.board.log.entries())
    s.team.pump()
    assert s.calls("own_subtask") == [] and s.board.tasks["task-2"]["route"] is None and not s.calls("dispatch")
    assert any("did not accept it" in i["text_fallback"] and "[no_owner_origin]" not in i["text_fallback"] for i in s.out)
    assert "submit_plan" in s.team.tools.view.menu()  # the director may ask again


def test_a_task_the_router_gives_no_origin_stays_unrouted(sy):
    s = sy(router_cls=NoSlot)
    d = planned(s, POST_A, SUBMIT, STOP)
    s.team.run(d)
    assert s.board.tasks["task-2"]["route"] is None and not s.calls("route") and not any(e.type == "board.origin" for e in s.board.log.entries())


def test_the_router_refuses_a_decomposition_whose_event_is_not_the_owners(sy):
    s = sy()
    ev = s.router.record_owner_decision("task-1", [{"id": "x", "type": "text", "sensitivity": "public"}], "chat_tool_call")  # not a human gesture
    assert s.router.events[ev]["tag"] == "CLAUDE-CODE"
    assert s.router.approve_decomposition("task-1", [{"id": "x", "type": "text", "sensitivity": "public"}], ev)["code"] == "no_owner_origin"
