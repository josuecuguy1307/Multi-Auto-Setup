"""BOARD (B1-B4): the typed board and the director (docs/BOARD_API.md, docs/NETWORK_CONTRACT.md sections 5, 6, 10). Each check ALSO runs on a fixture that violates it and must fail there."""
import sys

import boardrules
from conftest import ROOT

sys.path.insert(0, str(ROOT / "conformance" / "boardfixtures"))
sys.path.insert(0, str(ROOT / "tests"))
SRC = ROOT / "src" / "envrec_board"
FIX = ROOT / "conformance" / "boardfixtures"


# ---- B1: a model's tool call never produces an OWNER decision ---------------------------------------------------------------------------------

def test_B1_the_model_facing_module_cannot_reach_an_owner_decision():
    assert boardrules.model_facing_owner_sites(SRC / "director.py") == []


def test_B1_fails_on_a_model_tool_that_answers_a_decision():
    hits = " ".join(boardrules.model_facing_owner_sites(FIX / "model_tools_bad.py"))
    assert "imports the decision desk" in hits and ".answer(" in hits


def test_B1_driving_every_menu_action_and_approval_attempts_yields_no_owner_entry():
    from envrec_board.board import Board
    from envrec_board.decisions import DecisionDesk
    from envrec_board.director import ModelTools
    from envrec_board.log import MemoryLog
    b = Board(MemoryLog(), lambda: 1000.0)
    root = b.post_task("goal", "i", "o", "g", "internal", "owner", actor="OWNER")
    desk = DecisionDesk(b)
    d = desk.ask_reassign(root, "n2", "s")
    tools = ModelTools(b, root)
    for a in ({"action": "answer", "args": {"decision_id": d["decision_id"], "option": "approve"}}, {"action": "approve", "args": {}},
              {"action": "escalate_to_owner", "args": {"question": "q", "approve": True}}, {"action": "escalate_to_owner", "args": {"question": "q"}}):
        tools.act(a)
    assert [e for e in b.log.entries() if e.tag == "OWNER" and e.type != "board.task"] == []  # the owner's own root task is the only OWNER entry
    assert desk.pending() and desk.pending()[0]["decision_id"] == d["decision_id"]


# ---- B2: a confidential board payload never reaches a cloud chat -------------------------------------------------------------------------------

def test_B2_nothing_confidential_reaches_a_cloud_sink():
    from envrec_board.intents import IntentEmitter
    assert boardrules.confidential_reaches_cloud(IntentEmitter) == []


def test_B2_fails_on_an_emitter_that_sends_everything_to_the_cloud():
    from leaky_emitter import LeakyEmitter
    assert len(boardrules.confidential_reaches_cloud(LeakyEmitter)) >= 3


# ---- B3: the director never chooses a route or a machine ---------------------------------------------------------------------------------------

def test_B3_the_closed_menu_has_no_route_machine_or_model_argument():
    from envrec_board.director import SPEC
    assert boardrules.director_spec_argument_names(SPEC) == []


def test_B3_the_validator_refuses_every_route_machine_model_argument():
    from envrec_board.director import validate_action
    assert boardrules.route_arguments_accepted(validate_action) == []


def test_B3_fails_on_a_validator_that_lets_the_director_name_a_route():
    from lax_director import validate_action
    assert boardrules.route_arguments_accepted(validate_action) == ["route", "machine", "node_id", "model", "harness", "worker"]


def test_B3_the_static_check_fails_on_a_menu_with_a_machine_argument():
    assert boardrules.director_spec_argument_names({"post_subtask": {"task_type": str, "machine": str}}) == ["post_subtask.machine"]


# ---- B4: a subtask never runs without a router decision ---------------------------------------------------------------------------------------

def test_B4_a_task_without_a_router_decision_cannot_be_taken():
    from envrec_board.board import Board
    assert boardrules.unrouted_claim_succeeds(Board) is False


def test_B4_fails_on_a_board_that_lets_an_unrouted_task_run():
    from unrouted_board import UnroutedBoard
    assert boardrules.unrouted_claim_succeeds(UnroutedBoard) is True


# ---- B5: every cloud-bound intent goes through the router's outbound filter -----------------------------------------------------------------------

def test_B5_every_intent_for_a_cloud_chat_passes_the_outbound_filter():
    from envrec_board.intents import IntentEmitter
    assert boardrules.cloud_intents_bypassing_the_filter(IntentEmitter) == []


def test_B5_fails_on_an_emitter_that_skips_the_filter():
    from leaky_emitter import LeakyEmitter
    assert len(boardrules.cloud_intents_bypassing_the_filter(LeakyEmitter)) >= 2


# ---- B6: an escalation beyond what the owner pre-approved waits for the owner ---------------------------------------------------------------------

def test_B6_the_team_does_not_escalate_before_the_owner_answers():
    from envrec_board.team import TeamRun
    assert boardrules.escalation_runs_without_owner(TeamRun) is False


def test_B6_fails_on_a_team_that_escalates_by_itself():
    from auto_escalating_team import AutoEscalatingTeam
    assert boardrules.escalation_runs_without_owner(AutoEscalatingTeam) is True


# ---- B8: nothing in the board drives a model ---------------------------------------------------------------------------------------------------

def test_B8_the_board_package_imports_no_model_or_agent_loop():
    assert boardrules.model_driver_imports(SRC) == []


def test_B8_fails_on_board_code_that_drives_a_model():
    assert "imports envrec.model.scripted" in " ".join(boardrules.model_driver_imports(FIX / "drives_a_model.py"))


# ---- B7: only what the owner's plan lists runs ---------------------------------------------------------------------------------------------------

def test_B7_a_subtask_the_plan_does_not_list_is_never_sent():
    from envrec_board.team import TeamRun
    assert boardrules.unapproved_task_dispatched(TeamRun) is False


def test_B7_fails_on_a_team_that_runs_an_unlisted_subtask():
    from unapproved_team import UnapprovedTeam
    assert boardrules.unapproved_task_dispatched(UnapprovedTeam) is True
