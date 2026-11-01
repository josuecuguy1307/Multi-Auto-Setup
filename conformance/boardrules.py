"""Checks behind B1-B4 (docs/BOARD_API.md). Each takes the thing to measure as an argument so the same check runs on the real code and on a fixture that violates it."""
from __future__ import annotations

import ast
from pathlib import Path

from envrec_board.board import Board
from envrec_board.log import MemoryLog
from envrec_board.intents import Sink

def dec(tid, machine, step=0, env="llamacpp"):
    """A RouteDecision in the shape docs/ROUTER_API.md defines."""
    return {"task_id": tid, "route": {"environment_id": env, "subtype": "local_model", "model_id": "m", "access_id": "a", "machine_id": machine}, "rule_trace": [], "escalation_step": step,
            "evidence_label": "no data"}


OWNER_CALLS = {"answer", "reassign", "require", "ask", "ask_reassign"}  # DecisionDesk and gated Board operations: only a human channel reaches them


def model_facing_owner_sites(path: Path) -> list[str]:
    """B1 (static): a model-facing module imports the decisions module or calls an owner-decision operation."""
    out = []
    for p in ([path] if path.is_file() else sorted(path.rglob("*.py"))):
        tree = ast.parse(p.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "envrec_board.decisions":
                out.append(f"{p.name}:{n.lineno}: imports the decision desk")
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in OWNER_CALLS:
                out.append(f"{p.name}:{n.lineno}: calls .{n.func.attr}(")
    return out


def confidential_reaches_cloud(emitter_cls) -> list[dict]:
    """B2 (dynamic): what a cloud sink receives that carries or names a confidential task. Empty is the pass."""
    b = Board(MemoryLog(), lambda: 1000.0)
    cloud: list[dict] = []
    emitter_cls(b, None, [Sink("claude", "cloud", cloud.append)])
    tid = b.post_task("legal", "MARKER-CONFIDENTIAL-INPUT", "o", "g", "confidential", "owner", actor="MODEL")
    b.record_route_decision(tid, dec(tid, "n1"))
    lid = b.claim(tid, "n1", actor="SCRIPT")
    b.progress(lid, "step", "MARKER-CONFIDENTIAL-NOTE", actor="SCRIPT")
    return [i for i in cloud if i["sensitivity"] == "confidential" or "MARKER-CONFIDENTIAL" in str(i) or tid in str(i)]


def route_arguments_accepted(validate_action) -> list[str]:
    """B3 (dynamic): actions carrying a route, machine, model or harness that the validator lets through. Empty is the pass."""
    from envrec_board.director import DirectorView
    b = Board(MemoryLog(), lambda: 1000.0)
    root = b.post_task("goal", "i", "o", "g", "internal", "owner", actor="OWNER")
    view = DirectorView(b, root)
    base = {"task_type": "t", "input_ref": "i", "expected": "e", "test_ref": "g"}
    accepted = []
    for extra in ({"route": "cloud"}, {"machine": "n1"}, {"node_id": "n1"}, {"model": "big"}, {"harness": "x"}, {"worker": "n2"}):
        try:
            validate_action({"action": "post_subtask", "args": {**base, **extra}}, view)
            accepted.append(next(iter(extra)))
        except Exception:
            pass
    return accepted


def director_spec_argument_names(spec: dict) -> list[str]:
    """B3 (static): argument names in the closed menu that would let the model name a route, machine, model or harness."""
    from envrec_board.director import FORBIDDEN_ARGS
    return sorted(f"{a}.{k}" for a, args in spec.items() for k in args if any(f == k or f in k.split("_") for f in FORBIDDEN_ARGS))


def unrouted_claim_succeeds(board_cls) -> bool:
    """B4 (dynamic): a worker takes a task that has no router decision."""
    b = board_cls(MemoryLog(), lambda: 1000.0)
    tid = b.post_task("t", "i", "o", "g", "internal", "owner", actor="MODEL")
    try:
        b.claim(tid, "n1", actor="SCRIPT")
    except Exception:
        return False
    return bool(b.leases)


def cloud_intents_bypassing_the_filter(emitter_cls) -> list[dict]:
    """B5 (dynamic): intents a cloud sink received that the router's outbound filter never saw. Empty is the pass."""
    b = Board(MemoryLog(), lambda: 1000.0)
    cloud, seen = [], []

    def spy(i, chat):
        seen.append(i["id"])
        return i if i["sensitivity"] != "confidential" else dict(i, payload={}, text_fallback="Task update.", redacted=True)
    emitter_cls(b, None, [Sink("claude", "cloud", cloud.append)], outbound_filter=spy)
    b.post_task("t", "i", "o", "g", "public", "owner", actor="MODEL")
    b.post_task("t", "i", "o", "g", "confidential", "owner", actor="MODEL")
    return [i for i in cloud if i["id"] not in seen and not i["id"].endswith("-notice")]


def escalation_runs_without_owner(team_cls) -> bool:
    """B6 (dynamic): after a failed code grade the router proposes a higher step; True if the team ran it before the owner answered."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
    from board_sim import LoopbackModelServer, StandInRouter
    from envrec_board.decisions import DecisionDesk
    from envrec_board.team import TeamPolicy
    server = LoopbackModelServer(lambda m, p: "wrong")
    try:
        b = Board(MemoryLog(), lambda: 1000.0)
        router = StandInRouter(server.port, {"doc": "do it"})
        desk = DecisionDesk(b)
        root = b.post_task("goal", "i", "o", "g", "public", "owner", actor="OWNER")
        team = team_cls(b, desk, router, root_id=root, entry_chat={"kind": "local", "name": "panel"}, policy=TeamPolicy(preapproved_escalations=0))
        b.post_task("code", "doc", "e", "contains:NEVER", "public", "director", actor="MODEL", parent=root)
        plan = team.submit_plan()
        desk.answer(plan["decision_id"], "approve", channel="os_dialog", by="owner")
        team.pump()
        team.pump()
        return len([c for c in router.calls if c[0] == "dispatch"]) > 1
    finally:
        server.stop()


def model_driver_imports(path: Path) -> list[str]:
    """B8 (static): board code that imports something that drives a model or an agent loop, so the model could choose the route."""
    bad = ("envrec.model", "envrec.loop", "envrec.adapters", "envrec.protocols", "anthropic", "openai")
    out = []
    for p in ([path] if path.is_file() else sorted(path.rglob("*.py"))):
        for n in ast.walk(ast.parse(p.read_text())):
            mods = [n.module] if isinstance(n, ast.ImportFrom) and n.module else [a.name for a in n.names] if isinstance(n, ast.Import) else []
            out += [f"{p.name}:{n.lineno}: imports {m}" for m in mods if any(m == x or m.startswith(x + ".") for x in bad)]
    return out


def unapproved_task_dispatched(team_cls) -> bool:
    """B7 (dynamic): a subtask the owner's plan does not list is sent to a machine. Empty plan, one posted task: True means it ran anyway."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
    from board_sim import LoopbackModelServer, StandInRouter
    from envrec_board.decisions import DecisionDesk
    server = LoopbackModelServer(lambda m, p: "ok")
    try:
        b = Board(MemoryLog(), lambda: 1000.0)
        router = StandInRouter(server.port, {"doc": "do it"})
        root = b.post_task("goal", "i", "o", "g", "public", "owner", actor="OWNER")
        team = team_cls(b, DecisionDesk(b), router, root_id=root, entry_chat={"kind": "local", "name": "panel"})
        b.post_task("text", "doc", "e", "contains:ok", "public", "director", actor="MODEL", parent=root)
        team.pump()
        return any(c[0] == "dispatch" for c in router.calls)
    finally:
        server.stop()
