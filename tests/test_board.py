"""The typed board (docs/NETWORK_CONTRACT.md section 5): the six message types, leases, no double claims, replay, owner-only decisions, intents with Spanish fallbacks, the director's closed menu."""
import pytest

from envrec_board.board import Board
from envrec_board.decisions import DecisionDesk
from envrec_board.director import ModelTools, DirectorView, DirectorRefused, validate_action, NO_PROGRESS_TURNS
from envrec_board.intents import IntentEmitter, Sink, IntentInvalid, validate_intent
from envrec_board.log import JsonlLog, MemoryLog
from envrec_board.vocab import BoardError, MESSAGE_TYPES


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def dec(tid, machine, step=0, env="llamacpp"):
    """A RouteDecision in the shape docs/ROUTER_API.md defines."""
    return {"task_id": tid, "route": {"environment_id": env, "subtype": "local_model", "model_id": "m", "access_id": "a", "machine_id": machine}, "rule_trace": [], "escalation_step": step,
            "evidence_label": "no data"}


def mk(**kw):
    clock = Clock()
    return Board(MemoryLog(), clock, **kw), clock


def routed(b, node="n1", sensitivity="internal", task_type="summarize", **kw):
    tid = b.post_task(task_type, "in", "out", "grader", sensitivity, "owner-task", actor="MODEL", **kw)
    b.record_route_decision(tid, dec(tid, node))
    return tid


def codes(b):
    return [e.payload["refusal"] for e in b.log.entries() if e.type == "board.refused"]


# ---- the six message types ------------------------------------------------------------------------------------------------------------------

def test_the_board_has_exactly_the_six_contract_types():
    assert MESSAGE_TYPES == ("node_card", "task", "lease", "progress", "result", "published_skill")


def test_node_card_lists_only_tested_capabilities():
    b, _ = mk()
    card = {"node_id": "n1", "os_family": "windows", "hardware": {"ram_gib": 19.7}, "inference": "local", "harnesses": [], "capabilities": [{"name": "text", "evidence_id": "ev1", "date": "2026-10-30"}]}
    e = b.publish_node_card(card)
    assert e.type == "board.node_card" and b.cards["n1"]["capabilities"][0]["evidence_id"] == "ev1"
    bad = dict(card, node_id="n2", capabilities=[{"name": "vision"}])
    with pytest.raises(BoardError) as r:
        b.publish_node_card(bad)
    assert r.value.code == "untested_capability" and "n2" not in b.cards


def test_node_card_missing_field_is_refused():
    b, _ = mk()
    with pytest.raises(BoardError) as r:
        b.publish_node_card({"node_id": "n1"})
    assert r.value.code == "bad_entry"


def test_task_carries_envelope_metadata():
    b, _ = mk()
    tid = b.post_task("summarize", "in", "out", "grader", "confidential", "owner", actor="MODEL")
    e = [x for x in b.log.entries() if x.type == "board.task"][0]
    assert (e.task_id, e.task_type, e.sensitivity, e.actor, e.tag) == (tid, "summarize", "confidential", "MODEL", "MODEL")
    assert {"input", "expected", "test", "created_by"} <= set(e.payload)


def test_lease_progress_result_roundtrip_with_code_grader():
    b, _ = mk()
    tid = routed(b)
    lid = b.claim(tid, "n1", actor="SCRIPT")
    lease = [x for x in b.log.entries() if x.type == "board.lease"][0]
    assert {"lease_id", "node_id", "deadline", "attempt"} <= set(lease.payload) and lease.payload["attempt"] == 1
    b.progress(lid, "reading", "halfway", actor="SCRIPT")
    b.result(lid, "42", ["cap-1", "cap-2"], {"passed": True, "by": "code"}, actor="SCRIPT")
    t = b.tasks[tid]
    assert t["status"] == "done" and t["result"]["evidence_status"] == "tested" and t["lease_id"] is None
    assert b.leases[lid]["state"] == "ended"


def test_a_result_without_a_code_grade_is_a_hypothesis_not_tested():
    b, _ = mk()
    tid = routed(b)
    lid = b.claim(tid, "n1", actor="SCRIPT")
    b.result(lid, "the model says it works", [], {"passed": True, "by": "model"}, actor="SCRIPT")  # a model's own verdict is not a code grade
    assert b.tasks[tid]["status"] == "unverified" and b.tasks[tid]["result"]["evidence_status"] == "hypothesis"


def test_failed_grader_is_recorded_not_hidden():
    b, _ = mk()
    tid = routed(b)
    lid = b.claim(tid, "n1", actor="SCRIPT")
    b.result(lid, "x", [], {"passed": False, "by": "code"}, actor="SCRIPT")
    assert b.tasks[tid]["status"] == "failed"


def test_published_skill_needs_passed_tests():
    b, _ = mk()
    with pytest.raises(BoardError):
        b.publish_skill("s", "sha256:abc", "internal", tests_passed=False)
    b.publish_skill("s", "sha256:abc", "internal", tests_passed=True)
    assert "sha256:abc" in b.skills


def test_payload_key_that_could_carry_a_secret_is_refused():
    b, _ = mk()
    with pytest.raises(BoardError) as r:
        b.publish_node_card({"node_id": "n1", "os_family": "linux", "hardware": {}, "inference": "x", "harnesses": [], "capabilities": [], "api_token": "x"})
    assert r.value.code == "secret_key"


# ---- leases ---------------------------------------------------------------------------------------------------------------------------------

def test_double_claim_is_refused_and_the_first_lease_stays():
    b, _ = mk()
    tid = routed(b)
    lid = b.claim(tid, "n1", actor="SCRIPT")
    with pytest.raises(BoardError) as r:
        b.claim(tid, "n1", actor="SCRIPT")
    assert r.value.code == "already_claimed"
    assert b.tasks[tid]["lease_id"] == lid and len(b.leases) == 1


def test_concurrent_claims_produce_one_lease():
    import threading
    b, _ = mk()
    tid = routed(b)
    wins, errs = [], []

    def go():
        try:
            wins.append(b.claim(tid, "n1", actor="SCRIPT"))
        except BoardError as e:
            errs.append(e.code)
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1 and errs == ["already_claimed"] * 7


def test_expired_lease_frees_the_task_with_a_typed_reason():
    b, clock = mk()
    tid = routed(b)
    lid = b.claim(tid, "n1", actor="SCRIPT", lease_s=60)
    clock.t += 59
    assert b.sweep() == [] and b.tasks[tid]["status"] == "claimed"
    clock.t += 2
    freed = b.sweep()
    assert len(freed) == 1 and freed[0].type == "board.lease_expired" and freed[0].payload["reason"] == "deadline_passed"
    t = b.tasks[tid]
    assert t["status"] == "open" and t["free_reason"] == "deadline_passed" and t["lease_id"] is None and b.leases[lid]["state"] == "ended"


def test_work_on_an_expired_lease_is_refused_and_frees_the_task():
    b, clock = mk()
    tid = routed(b)
    lid = b.claim(tid, "n1", actor="SCRIPT", lease_s=10)
    clock.t += 11
    with pytest.raises(BoardError) as r:
        b.result(lid, "late", [], {"passed": True, "by": "code"}, actor="SCRIPT")
    assert r.value.code == "lease_expired" and b.tasks[tid]["status"] == "open" and b.tasks[tid]["result"] is None


def test_a_freed_task_can_be_taken_again_with_attempt_two():
    b, clock = mk()
    tid = routed(b)
    b.claim(tid, "n1", actor="SCRIPT", lease_s=5)
    clock.t += 6
    b.claim(tid, "n1", actor="SCRIPT")  # claim sweeps first
    assert b.tasks[tid]["attempt"] == 2 and b.tasks[tid]["status"] == "claimed"


def test_attempts_are_capped():
    b, clock = mk(max_attempts=2)
    tid = routed(b)
    for _ in range(2):
        b.claim(tid, "n1", actor="SCRIPT", lease_s=5)
        clock.t += 6
        b.sweep()
    assert b.tasks[tid]["status"] == "exhausted" and b.tasks[tid]["free_reason"] == "attempts_exhausted"
    with pytest.raises(BoardError) as r:
        b.claim(tid, "n1", actor="SCRIPT")
    assert r.value.code == "not_open"


def test_progress_extends_the_deadline_within_the_maximum_only():
    b, _ = mk(max_extend_s=100)
    tid = routed(b)
    lid = b.claim(tid, "n1", actor="SCRIPT", lease_s=60)
    d0 = b.leases[lid]["deadline"]
    b.progress(lid, "s", "n", actor="SCRIPT", extend_s=80)
    assert b.leases[lid]["deadline"] == d0 + 80
    with pytest.raises(BoardError) as r:
        b.progress(lid, "s", "n", actor="SCRIPT", extend_s=30)
    assert r.value.code == "extension_too_long" and b.leases[lid]["deadline"] == d0 + 80


def test_lease_longer_than_the_maximum_is_refused():
    b, _ = mk(max_lease_s=100)
    tid = routed(b)
    with pytest.raises(BoardError) as r:
        b.claim(tid, "n1", actor="SCRIPT", lease_s=500)
    assert r.value.code == "lease_too_long" and b.tasks[tid]["status"] == "open"


def test_release_frees_the_task_with_the_reason():
    b, _ = mk()
    tid = routed(b)
    lid = b.claim(tid, "n1", actor="SCRIPT")
    b.release(lid, "released_by_worker", actor="SCRIPT")
    assert b.tasks[tid]["status"] == "open" and b.tasks[tid]["free_reason"] == "released_by_worker"


# ---- the board does not route ---------------------------------------------------------------------------------------------------------------

def test_a_task_without_a_router_decision_cannot_be_taken():
    b, _ = mk()
    tid = b.post_task("summarize", "i", "o", "g", "internal", "owner", actor="MODEL")
    with pytest.raises(BoardError) as r:
        b.claim(tid, "n1", actor="SCRIPT")
    assert r.value.code == "no_router_decision" and not b.leases


def test_only_the_machine_the_router_named_can_take_it():
    b, _ = mk()
    tid = routed(b, node="n1")
    with pytest.raises(BoardError) as r:
        b.claim(tid, "n2", actor="SCRIPT")
    assert r.value.code == "wrong_node_for_route"


def test_a_malformed_route_decision_is_refused():
    b, _ = mk()
    tid = b.post_task("summarize", "i", "o", "g", "internal", "owner", actor="MODEL")
    with pytest.raises(BoardError):
        b.record_route_decision(tid, {"task_id": tid, "route": {}, "rule_trace": [], "escalation_step": 0, "evidence_label": "x"})


def test_verifier_cannot_be_the_machine_that_did_the_work():
    b, _ = mk()
    tid = routed(b, node="n1")
    lid = b.claim(tid, "n1", actor="SCRIPT")
    b.result(lid, "x", [], None, actor="SCRIPT")
    v = routed(b, node="n1", task_type="verification", verifies=tid)
    with pytest.raises(BoardError) as r:
        b.claim(v, "n1", actor="SCRIPT")
    assert r.value.code == "verifier_not_independent"
    w = routed(b, node="n2", task_type="verification", verifies=tid)
    assert b.claim(w, "n2", actor="SCRIPT")


def test_a_derived_task_cannot_lower_sensitivity():
    b, _ = mk()
    root = b.post_task("goal", "i", "o", "g", "confidential", "owner", actor="MODEL")
    with pytest.raises(BoardError) as r:
        b.post_task("sub", "i", "o", "g", "public", "director", actor="MODEL", parent=root)
    assert r.value.code == "sensitivity_lowered"
    sub = b.post_task("sub", "i", "o", "g", "confidential", "director", actor="MODEL", parent=root)
    assert b.tasks[sub]["sensitivity"] == "confidential"


def test_refusals_are_recorded_as_guard_entries():
    b, _ = mk()
    tid = routed(b)
    b.claim(tid, "n1", actor="SCRIPT")
    with pytest.raises(BoardError):
        b.claim(tid, "n1", actor="SCRIPT")
    last = [e for e in b.log.entries() if e.type == "board.refused"][-1]
    assert last.tag == "GUARD" and last.payload["refusal"] == "already_claimed"


# ---- append-only and replay ---------------------------------------------------------------------------------------------------------------------

def busy_board():
    b, clock = mk()
    b.publish_node_card({"node_id": "n1", "os_family": "macos", "hardware": {}, "inference": "local", "harnesses": [], "capabilities": []})
    t1, t2 = routed(b), routed(b, node="n2")
    l1 = b.claim(t1, "n1", actor="SCRIPT", lease_s=30)
    b.progress(l1, "a", "b", actor="SCRIPT", extend_s=10)
    b.result(l1, "o", ["e"], {"passed": False, "by": "code"}, actor="SCRIPT")
    b.claim(t2, "n2", actor="SCRIPT", lease_s=5)
    clock.t += 6
    b.sweep()
    with pytest.raises(BoardError):
        b.claim(t2, "n1", actor="SCRIPT")
    return b


def test_replay_rebuilds_the_same_state_without_a_clock():
    b = busy_board()
    entries = b.log.entries()
    r = Board.replay(entries, clock=lambda: (_ for _ in ()).throw(AssertionError("replay read the clock")))
    assert r.snapshot() == b.snapshot()


def test_replay_of_a_prefix_is_the_state_at_that_moment():
    b = busy_board()
    es = b.log.entries()
    cut = next(i for i, e in enumerate(es) if e.type == "board.result")
    assert Board.replay(es[:cut]).tasks["task-1"]["status"] == "claimed"
    assert Board.replay(es[:cut + 1]).tasks["task-1"]["status"] == "failed"


def test_the_log_has_no_way_to_change_or_remove_an_entry():
    for cls in (MemoryLog, JsonlLog):
        assert {m for m in dir(cls) if not m.startswith("_")} <= {"append", "entries", "path"}


def test_jsonl_log_survives_a_restart(tmp_path):
    clock = Clock()
    b = Board(JsonlLog(tmp_path / "board.jsonl"), clock)
    tid = routed(b)
    b.claim(tid, "n1", actor="SCRIPT")
    again = Board(JsonlLog(tmp_path / "board.jsonl"), clock)
    assert again.snapshot() == b.snapshot() and again.tasks[tid]["status"] == "claimed"
    assert [e.n for e in again.log.entries()] == list(range(1, len(again.log.entries()) + 1))


# ---- owner decisions -------------------------------------------------------------------------------------------------------------------------

def desk_with_claimed():
    b, clock = mk()
    tid = routed(b, node="n1")
    b.claim(tid, "n1", actor="SCRIPT")
    return b, DecisionDesk(b), tid, clock


def test_reassign_needs_the_owners_approval_of_exactly_that_change():
    b, desk, tid, _ = desk_with_claimed()
    d = desk.ask_reassign(tid, "n2", "Move the task to n2?")
    with pytest.raises(BoardError) as r:
        desk.reassign(tid, "n2", d["decision_id"])
    assert r.value.code == "no_owner_approval" and b.tasks[tid]["node_id"] == "n1"
    desk.answer(d["decision_id"], "approve", channel="os_dialog", by="owner")
    desk.reassign(tid, "n2", d["decision_id"])
    assert b.tasks[tid]["node_id"] == "n2" and b.tasks[tid]["attempt"] == 2


def test_an_approval_is_bound_to_the_exact_details_and_used_once():
    b, desk, tid, _ = desk_with_claimed()
    d = desk.ask_reassign(tid, "n2", "s")
    desk.answer(d["decision_id"], "approve", channel="panel", by="owner")
    with pytest.raises(BoardError):
        desk.reassign(tid, "n3", d["decision_id"])  # approved n2, not n3
    desk.reassign(tid, "n2", d["decision_id"])
    with pytest.raises(BoardError) as r:
        desk.reassign(tid, "n2", d["decision_id"])  # used
    assert r.value.code == "no_owner_approval"


def test_a_rejection_does_not_approve():
    b, desk, tid, _ = desk_with_claimed()
    d = desk.ask_reassign(tid, "n2", "s")
    desk.answer(d["decision_id"], "reject", channel="host_dialog", by="owner")
    with pytest.raises(BoardError):
        desk.reassign(tid, "n2", d["decision_id"])
    assert b.tasks[tid]["node_id"] == "n1"


@pytest.mark.parametrize("channel,by", [("chat_tool_call", "owner"), ("", "owner"), ("os_dialog", "model"), ("os_dialog", "")])
def test_an_answer_not_from_a_human_channel_is_refused_and_never_owner(channel, by):
    b, desk, tid, _ = desk_with_claimed()
    d = desk.ask_reassign(tid, "n2", "s")
    with pytest.raises(BoardError) as r:
        desk.answer(d["decision_id"], "approve", channel=channel, by=by)
    assert r.value.code == "not_a_human_channel"
    assert not [e for e in b.log.entries() if e.tag == "OWNER"]
    with pytest.raises(BoardError):
        desk.reassign(tid, "n2", d["decision_id"])


def test_the_operators_answer_is_recorded_apart_and_does_not_count():
    b, desk, tid, _ = desk_with_claimed()
    d = desk.ask_reassign(tid, "n2", "s")
    e = desk.answer(d["decision_id"], "approve", channel="panel", by="claude-code")
    assert e.tag == "CLAUDE-CODE"
    with pytest.raises(BoardError) as r:
        desk.reassign(tid, "n2", d["decision_id"])
    assert r.value.code == "no_owner_approval"
    assert desk.pending()[0]["decision_id"] == d["decision_id"]  # still waiting for the owner


def test_a_forged_approval_entry_does_not_open_the_gate():
    from envrec_board.vocab import Entry
    b, desk, tid, _ = desk_with_claimed()
    fake = Entry(n=1, type="board.decision_answered", tag="OWNER", task_id=tid, task_type="x", sensitivity="internal", actor="OWNER", payload={"option": "approve"})
    with pytest.raises(BoardError) as r:
        b.reassign(tid, "n2", approval_event=fake)
    assert r.value.code == "no_owner_approval"


def test_a_decision_nobody_answers_expires_and_changes_nothing():
    b, desk, tid, clock = desk_with_claimed()
    d = desk.ask_reassign(tid, "n2", "s")
    clock.t += 901
    with pytest.raises(BoardError) as r:
        desk.answer(d["decision_id"], "approve", channel="panel", by="owner")
    assert r.value.code == "decision_expired" and desk.pending() == [] and b.tasks[tid]["node_id"] == "n1"


def test_a_machine_write_decision_shows_its_command_and_undo():
    b, desk, tid, _ = desk_with_claimed()
    with pytest.raises(BoardError):
        desk.ask("machine_write", tid, "s", {"step": 1})
    d = desk.ask("machine_write", tid, "Install", {"step": 1}, command="mas install", undo="mas uninstall")
    assert d["writes"] and d["hash"].startswith("sha256:")


# ---- intents ---------------------------------------------------------------------------------------------------------------------------------

def english(i):
    t = i["text_fallback"]
    return bool(t) and any(w in t.lower() for w in ("task", "board", "owner", "decision", "router", "director", "network")) and not any(ch in t for ch in "áéíóúñ¿¡")


def test_every_board_change_emits_valid_intents_with_a_spanish_fallback():
    b, clock = mk()
    desk = DecisionDesk(b)
    out = []
    em = IntentEmitter(b, desk, [Sink("panel", "local", out.append)])
    tid = routed(b)
    lid = b.claim(tid, "n1", actor="SCRIPT")
    b.progress(lid, "reading", "halfway", actor="SCRIPT")
    b.result(lid, "x", [], {"passed": True, "by": "code"}, actor="SCRIPT")
    states = [i["payload"].get("state") for i in out if i["kind"] == "progress"]
    assert states == ["posted", "routed", "taken", "running", "done"]
    assert {i["kind"] for i in out} == {"progress", "show_data"}
    assert all(english(i) for i in out) and all(validate_intent(i) for i in out)
    board = [i for i in out if i["kind"] == "show_data"][-1]
    assert board["payload"]["data_kind"] == "board" and "task-1" in board["text_fallback"]
    assert "input" not in board["payload"]["board"]["tasks"][0]  # the compact board carries no task content


def test_text_fallback_alone_tells_the_state_of_a_failed_grade_and_an_expiry():
    b, clock = mk()
    out = []
    IntentEmitter(b, None, [Sink("panel", "local", out.append)])
    t1 = routed(b)
    l1 = b.claim(t1, "n1", actor="SCRIPT")
    b.result(l1, "x", [], {"passed": False, "by": "code"}, actor="SCRIPT")
    t2 = routed(b)
    b.claim(t2, "n1", actor="SCRIPT", lease_s=5)
    clock.t += 6
    b.sweep()
    texts = " | ".join(i["text_fallback"] for i in out)
    assert "did NOT pass its code test" in texts and "the deadline passed" in texts


def test_a_refusal_becomes_a_text_intent_in_spanish():
    b, _ = mk()
    out = []
    IntentEmitter(b, None, [Sink("panel", "local", out.append)])
    tid = routed(b)
    b.claim(tid, "n1", actor="SCRIPT")
    with pytest.raises(BoardError):
        b.claim(tid, "n1", actor="SCRIPT")
    t = [i for i in out if i["kind"] == "text"]
    assert t and t[-1]["payload"]["code"] == "already_claimed" and "already holds that task" in t[-1]["text_fallback"]


def test_owner_decisions_become_confirm_and_ask_choice_with_self_sufficient_text():
    b, _ = mk()
    desk = DecisionDesk(b)
    out = []
    IntentEmitter(b, desk, [Sink("panel", "local", out.append)])
    tid = routed(b)
    desk.ask_reassign(tid, "n2", "Move the task to n2?")
    desk.ask("machine_write", tid, "Install runtime", {"s": 1}, command="mas install", undo="mas uninstall")
    desk.ask("escalate_beyond_preapproved", tid, "Where should it go next?", {"x": 1}, options=[{"id": "a", "label": "Another machine"}, {"id": "b", "label": "Stop"}])
    c = [i for i in out if i["kind"] in ("confirm", "ask_choice")]
    assert [i["kind"] for i in c] == ["confirm", "confirm", "ask_choice"]
    assert c[0]["payload"]["writes"] is False and c[1]["payload"]["writes"] is True
    assert "Exact command: mas install" in c[1]["text_fallback"] and "How to undo it: mas uninstall" in c[1]["text_fallback"]
    assert "1. Another machine" in c[2]["text_fallback"] and "2. Stop" in c[2]["text_fallback"]


def test_the_operators_answer_is_announced_as_not_counting():
    b, _ = mk()
    desk = DecisionDesk(b)
    out = []
    IntentEmitter(b, desk, [Sink("panel", "local", out.append)])
    tid = routed(b)
    d = desk.ask_reassign(tid, "n2", "s")
    desk.answer(d["decision_id"], "approve", channel="panel", by="claude-code")
    assert "does not count" in out[-1]["text_fallback"] and out[-1]["actor"] == "GUARD"


def test_validator_rejects_an_intent_without_text_and_a_write_without_undo():
    base = {"id": "x", "seq": 1, "ts": 0, "kind": "text", "run_id": "r", "actor": "GUARD", "sensitivity": "public", "payload": {}, "text_fallback": " "}
    with pytest.raises(IntentInvalid):
        validate_intent(base)
    with pytest.raises(IntentInvalid):
        validate_intent(dict(base, kind="confirm", text_fallback="x", payload={"writes": True, "summary": "s", "command": "c"}))
    with pytest.raises(IntentInvalid):
        validate_intent(dict(base, kind="show_data", text_fallback="x", payload={"data_kind": "plan"}))


def test_confidential_never_reaches_a_cloud_sink_and_the_owner_gets_a_notice():
    b, _ = mk()
    local, cloud = [], []
    em = IntentEmitter(b, None, [Sink("webui", "local", local.append), Sink("claude", "cloud", cloud.append)])
    tid = b.post_task("legal", "SECRETO-CONTRATO", "o", "g", "confidential", "owner", actor="MODEL")
    assert any(i["sensitivity"] == "confidential" for i in local)
    assert cloud and all(i["sensitivity"] == "public" and i["payload"] == {"code": "withheld"} for i in cloud)
    assert "SECRETO" not in str(cloud) and tid not in str(cloud)
    assert {w["reason"] for w in em.withheld} == {"confidential"}


def test_a_cloud_sink_gets_only_public_until_the_outbound_filter_is_installed():
    b, _ = mk()
    cloud = []
    em = IntentEmitter(b, None, [Sink("claude", "cloud", cloud.append)])
    b.post_task("t", "i", "o", "g", "internal", "owner", actor="MODEL")
    assert all(i["payload"] == {"code": "withheld"} for i in cloud) and {w["reason"] for w in em.withheld} == {"no_outbound_filter"}
    cloud.clear()
    b2, _ = mk()
    em2 = IntentEmitter(b2, None, [Sink("claude", "cloud", cloud.append)], outbound_filter=lambda i, chat: dict(i, payload={"state": "posted"}))
    b2.post_task("t", "i", "o", "g", "internal", "owner", actor="MODEL")
    assert cloud and cloud[0]["payload"] == {"state": "posted"}


def test_every_cloud_intent_goes_through_the_outbound_filter_and_confidential_leaves_only_redacted():
    b, _ = mk()
    cloud, seen = [], []

    def flt(i, chat):  # the shape of ROUTER_API section 5 for cloud + confidential
        seen.append((i["sensitivity"], chat))
        if i["sensitivity"] == "confidential":
            return dict(i, payload={"task_id": i["payload"].get("task_id"), "state": i["payload"].get("state"), "machine": None}, text_fallback="Task update.", redacted=True)
        return i
    em = IntentEmitter(b, None, [Sink("claude", "cloud", cloud.append)], outbound_filter=flt)
    b.post_task("t", "i", "o", "g", "internal", "owner", actor="MODEL")
    b.post_task("t", "SECRET-TEXT", "o", "g", "confidential", "owner", actor="MODEL")
    assert any(s == "confidential" for s, _ in seen) and all(chat == {"kind": "cloud", "name": "claude"} for _, chat in seen)
    assert "SECRET-TEXT" not in str(cloud) and any(i.get("redacted") for i in cloud)
    assert em.withheld == []


def test_a_confidential_intent_the_filter_did_not_redact_is_withheld():
    b, _ = mk()
    cloud = []
    em = IntentEmitter(b, None, [Sink("claude", "cloud", cloud.append)], outbound_filter=lambda i, chat: i)  # a broken filter that returns it unchanged
    b.post_task("t", "SECRET-TEXT", "o", "g", "confidential", "owner", actor="MODEL")
    assert "SECRET-TEXT" not in str(cloud) and {w["reason"] for w in em.withheld} == {"not_redacted"}


# ---- the director ---------------------------------------------------------------------------------------------------------------------------

def director(sensitivity="internal"):
    b, clock = mk()
    root = b.post_task("goal", "move", "ready", "g", sensitivity, "owner", actor="OWNER")
    return b, root, ModelTools(b, root), clock


def sub_result(b, tid, node, passed):
    b.record_route_decision(tid, dec(tid, node))
    lid = b.claim(tid, node, actor="SCRIPT")
    b.result(lid, "o", ["e"], {"passed": passed, "by": "code"} if passed is not None else None, actor="SCRIPT")


def test_menu_at_the_start_offers_only_posting_and_escalation():
    b, root, tools, _ = director()
    assert list(tools.view.menu()) == ["post_subtask", "escalate_to_owner"]


def test_post_subtask_creates_a_typed_child_with_the_roots_sensitivity_and_no_route():
    b, root, tools, _ = director("confidential")
    r = tools.act({"action": "post_subtask", "args": {"task_type": "summarize", "input_ref": "doc-1", "expected": "summary", "test_ref": "g1"}})
    t = b.tasks[r["task_id"]]
    assert r["ok"] and t["parent"] == root and t["sensitivity"] == "confidential" and t["route"] is None
    with pytest.raises(BoardError):
        b.claim(r["task_id"], "n1", actor="SCRIPT")  # nobody can take it until the router decides


@pytest.mark.parametrize("arg", ["route", "machine", "node_id", "model", "harness", "worker", "sensitivity", "decision"])
def test_the_director_cannot_pass_a_route_machine_model_or_sensitivity(arg):
    b, root, tools, _ = director()
    r = tools.act({"action": "post_subtask", "args": {"task_type": "t", "input_ref": "i", "expected": "e", "test_ref": "g", arg: "x"}})
    assert not r["ok"] and r["code"] == "forbidden_argument" and len(b.tasks) == 1


@pytest.mark.parametrize("action,code", [
    ({"action": "run_shell", "args": {}}, "unknown_action"),
    ({"action": "post_subtask", "args": {"task_type": "t"}}, "missing_argument"),
    ({"action": "post_subtask", "args": {"task_type": "t", "input_ref": "i", "expected": "e", "test_ref": ""}}, "bad_argument"),
    ({"action": "request_verification", "args": {"subtask_id": 9}}, "bad_argument"),
    ({"action": "finish", "args": {"summary": "s"}}, "not_offered"),
    ({"action": "accept_result", "args": {"subtask_id": "task-9"}}, "not_offered"),
    ("post_subtask", "bad_shape"),
    ({"action": "post_subtask", "args": {"task_type": "t", "input_ref": "i", "expected": "e", "test_ref": "g"}, "extra": 1}, "bad_shape"),
])
def test_menu_validation_is_typed(action, code):
    b, root, tools, _ = director()
    assert tools.act(action)["code"] == code


def test_unverified_result_cannot_be_accepted_until_a_different_machine_verifies_it():
    b, root, tools, _ = director()
    tid = tools.act({"action": "post_subtask", "args": {"task_type": "summarize", "input_ref": "d", "expected": "e", "test_ref": "g"}})["task_id"]
    sub_result(b, tid, "n1", None)  # a model-written result: hypothesis
    m = tools.view.menu()
    assert "accept_result" not in m and m["request_verification"]["subtask_ids"] == [tid]
    assert tools.act({"action": "accept_result", "args": {"subtask_id": tid}})["code"] == "not_offered"
    v = tools.act({"action": "request_verification", "args": {"subtask_id": tid}})["task_id"]
    assert b.tasks[v]["verifies"] == tid
    sub_result(b, v, "n2", True)
    assert tools.act({"action": "accept_result", "args": {"subtask_id": tid}})["ok"]


def test_code_graded_result_can_be_accepted_and_failed_one_cannot():
    b, root, tools, _ = director()
    a = tools.act({"action": "post_subtask", "args": {"task_type": "x", "input_ref": "d", "expected": "e", "test_ref": "g"}})["task_id"]
    c = tools.act({"action": "post_subtask", "args": {"task_type": "y", "input_ref": "d", "expected": "e", "test_ref": "g"}})["task_id"]
    sub_result(b, a, "n1", True)
    sub_result(b, c, "n1", False)
    assert set(tools.view.menu()["accept_result"]["subtask_ids"]) == {a}
    assert tools.act({"action": "accept_result", "args": {"subtask_id": c}})["code"] == "not_offered"


def test_finish_is_offered_only_when_every_subtask_is_decided_and_it_closes_the_protocol():
    b, root, tools, _ = director()
    a = tools.act({"action": "post_subtask", "args": {"task_type": "x", "input_ref": "d", "expected": "e", "test_ref": "g"}})["task_id"]
    sub_result(b, a, "n1", True)
    assert "finish" not in tools.view.menu()
    tools.act({"action": "accept_result", "args": {"subtask_id": a}})
    assert "finish" in tools.view.menu()
    r = tools.act({"action": "finish", "args": {"summary": "done"}})
    assert r["ok"] and r["stop"] == "finished" and tools.stop == "finished"
    assert tools.act({"action": "escalate_to_owner", "args": {"question": "q"}})["code"] == "protocol_closed"


def test_escalate_to_owner_closes_as_blocked_needs_human():
    b, root, tools, _ = director()
    assert tools.act({"action": "escalate_to_owner", "args": {"question": "continue?"}})["stop"] == "blocked_needs_human"


def test_repeated_identical_actions_are_not_progress_and_end_as_no_progress():
    b, root, tools, _ = director()
    act = {"action": "post_subtask", "args": {"task_type": "x", "input_ref": "d", "expected": "e", "test_ref": "g"}}
    assert tools.act(act)["ok"]
    n = len(b.tasks)
    results = [tools.act(dict(act)) for _ in range(NO_PROGRESS_TURNS)]
    assert all(r["code"] == "repeated_action" and not r["progress"] for r in results)
    assert results[-1]["stop"] == "no_progress" and tools.stop == "no_progress" and len(b.tasks) == n


def test_a_real_action_resets_the_idle_count():
    b, root, tools, _ = director()
    for _ in range(NO_PROGRESS_TURNS - 1):
        tools.act({"action": "nope", "args": {}})
    assert tools.act({"action": "post_subtask", "args": {"task_type": "x", "input_ref": "d", "expected": "e", "test_ref": "g"}})["ok"] and tools.idle_turns == 0


def test_director_actions_are_tagged_model_and_refusals_guard():
    b, root, tools, _ = director()
    tools.act({"action": "nope", "args": {}})
    tid = tools.act({"action": "post_subtask", "args": {"task_type": "x", "input_ref": "d", "expected": "e", "test_ref": "g"}})["task_id"]
    refused = [e for e in b.log.entries() if e.type == "board.director_refused"]
    assert [e.tag for e in refused] == ["GUARD"] and refused[0].payload["refusal"] == "unknown_action"
    assert [e.tag for e in b.log.entries() if e.type == "board.task" and e.task_id == tid] == ["MODEL"]


def test_the_model_tools_surface_has_no_owner_or_routing_methods():
    public = {m for m in dir(ModelTools) if not m.startswith("_")}
    assert public == {"act"}


def test_director_intents_in_spanish():
    b, root, tools, _ = director()
    out = []
    IntentEmitter(b, None, [Sink("panel", "local", out.append)])
    tools.act({"action": "escalate_to_owner", "args": {"question": "Shall I continue on the other machine?"}})
    assert "The director has a question for you: Shall I continue on the other machine?" in out[-1]["text_fallback"]


def test_require_itself_ignores_the_operators_approval():
    b, desk, tid, _ = desk_with_claimed()
    d = desk.ask_reassign(tid, "n2", "s")
    desk.answer(d["decision_id"], "approve", channel="panel", by="claude-code")
    with pytest.raises(BoardError) as r:
        desk.require(d["decision_id"], "reassign", tid, {"to_node": "n2"})
    assert r.value.code == "no_owner_approval"
