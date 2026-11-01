"""SIMULATED-LOCAL end to end (ports 8043/8044, state ~/mas-t4b-state): owner task -> director splits into 2 subtasks -> workers claim -> one fails its code grader -> the router escalates (the owner approves)
-> cross verification on another machine -> the director finishes. The router is the stand-in of tests/board_sim.py (the approved API's shapes), the model server is a real loopback HTTP server,
the director is scripted. The intent stream is written as the owner would read it in a chat. CLAUDE-CODE events: 0."""
import os
from pathlib import Path

from board_sim import LoopbackModelServer, StandInRouter
from envrec_board.board import Board
from envrec_board.decisions import DecisionDesk
from envrec_board.intents import IntentEmitter, Sink
from envrec_board.log import JsonlLog
from envrec_board.team import TeamRun
from test_board_team import POST_A, POST_B, STORE, SUBMIT, VERIFY, Script, answers

STATE = Path(os.environ.get("MAS_T4B_STATE", Path.home() / "mas-t4b-state"))


def test_end_to_end_run_with_an_escalation_and_a_cross_verification():
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "board.jsonl").unlink(missing_ok=True)
    server = LoopbackModelServer(answers, port=8044)
    try:
        board = Board(JsonlLog(STATE / "board.jsonl"))
        router = StandInRouter(server.port, dict(STORE))
        desk = DecisionDesk(board)
        stream, cloud = [], []
        IntentEmitter(board, desk, [Sink("owner-panel", "local", stream.append), Sink("claude-desktop", "cloud", cloud.append)], outbound_filter=router.filter_outbound)
        root = board.post_task("goal", "Prepare my notes", "all notes ready", "owner", "public", "owner", actor="OWNER")
        team = TeamRun(board, desk, router, root_id=root, entry_chat={"kind": "local", "name": "owner-panel"})
        director = Script(POST_A, POST_B, SUBMIT, VERIFY("task-3"),
                          {"action": "accept_result", "args": {"subtask_id": "task-2"}},
                          {"action": "accept_result", "args": {"subtask_id": "task-3"}},
                          {"action": "finish", "args": {"summary": "both notes are ready and one was cross-verified"}})
        gestures = []

        def owner(kind):
            d = next(p for p in desk.pending() if p["kind"] == kind)
            desk.answer(d["decision_id"], "approve", channel="os_dialog", by="owner")
            gestures.append(f"OWNER (system approval dialog) approved {d['decision_id']}: {d['summary']}")

        r = team.run(director)
        assert r["state"] == "waiting_for_owner" and not router.calls  # the director's plan is with the owner; nothing was registered or routed
        owner("decompose")
        r = team.run(director)  # subtask 3 fails its code grader on lenovo; the router's next step is inside the limit, so it runs and the owner is only told
        assert r == {"state": "stopped", "stop": "finished"} and not [p for p in desk.pending() if p["kind"] == "escalate_beyond_preapproved"]

        t2, t3, t4 = (board.tasks[i] for i in ("task-2", "task-3", "task-4"))
        assert (t2["status"], board.machine_of("task-2")) == ("done", "lenovo")
        assert [x["node_id"] for x in t3["results"]] == ["lenovo", "mac"] and t3["status"] == "done" and t3["escalation_step"] == 1
        assert t4["verifies"] == "task-3" and t4["status"] == "done" and t4["worked_by"] == "lenovo" != t3["worked_by"]
        assert t4["exclude"] == {"machine_ids": ["mac"], "environment_ids": ["claude-code"]} and router.tasks["task-4"]["exclude"] == t4["exclude"]
        assert len([c for c in router.calls if c[0] == "approve_decomposition"]) == 1 and [c[1] for c in router.calls if c[0] == "own_subtask"] == ["task-2", "task-3", "task-4"]
        assert [c for c in router.calls if c[0] == "report_result"] == [("report_result", "task-2", 0, True), ("report_result", "task-3", 0, False), ("report_result", "task-3", 1, True),
                                                                        ("report_result", "task-4", 0, True)]  # verification passed on lenovo, a different machine than mac
        assert not [e for e in board.log.entries() if e.tag == "CLAUDE-CODE"] and Board.replay(board.log.entries()).snapshot() == board.snapshot()
        assert all(r["sensitivity"] == "public" for r in [i for i in cloud])  # public progress passes through the router's filter unchanged

        lines = ["# SIMULATED-LOCAL end-to-end run: what the owner reads in the chat", "# router: stand-in of docs/ROUTER_API.md; model server: real HTTP on 127.0.0.1; director: scripted; CLAUDE-CODE events: 0", ""]
        shown = {"progress", "confirm", "ask_choice", "text"}
        for i in stream:
            if i["kind"] in shown:
                lines.append(f"[{i['kind']} / {i['actor']}] {i['text_fallback']}")
        lines += ["", "# owner gestures", *gestures, "", "# last board"]
        lines.append([i for i in stream if i["kind"] == "show_data"][-1]["text_fallback"])
        (STATE / "e2e-stream.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        text = "\n".join(lines)
        assert "did NOT pass its code test" in text and "escalation step 1" in text and "finished the work" in text
    finally:
        server.stop()
