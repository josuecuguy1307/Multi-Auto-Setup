"""The board (director and workers, docs/BOARD_API.md) wired to the real router. The board was built against a router that answers at once; the real one sends a task to a computer and the answer comes
later. `LoopRouterPort` is the seam: it speaks the board's plain-dict port, runs every router call on the event loop (where the coordinator's state and log live) and makes `dispatch` wait for the
node's task to end, so the board's synchronous pump can run in a worker thread. It decides nothing: routes, escalations, grades and approvals are the router's, the owner's and the code's."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
from typing import Any

from envrec_coordinator.router import RouteDecision, Task
from envrec_coordinator.router.model import TaskHandle

WAIT_S = 1500.0


class LoopRouterPort:
    def __init__(self, services, loop: asyncio.AbstractEventLoop) -> None:
        self.s, self.loop = services, loop

    def _on_loop(self, fn, *a):
        try:
            if asyncio.get_running_loop() is self.loop:
                return fn(*a)
        except RuntimeError:
            pass
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            try:
                fut.set_result(fn(*a))
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)
        self.loop.call_soon_threadsafe(run)
        return fut.result(60)

    @property
    def router(self):
        return self.s.router

    @staticmethod
    def _dump(x) -> dict:
        return x.model_dump() if hasattr(x, "model_dump") else x

    # ---- the RouterPort of envrec_board ------------------------------------------------------------------------------------------------------------

    def route(self, task: dict) -> dict:
        ref = str(task["payload_ref"])
        if not ref.startswith(("result-of:", "instr:", "payload:", "board:")):  # a director names its input as text or as a name; the text is what the router sends
            key = "board:" + hashlib.sha256(ref.encode()).hexdigest()[:16]
            self.s.payloads.setdefault(key, self.s.payloads.get(ref, ref))
            task = {**task, "payload_ref": key}
        return self._dump(self._on_loop(lambda: self.router.route(Task(**task))))

    def dispatch(self, decision: dict) -> dict:
        d = RouteDecision.model_validate(decision)
        h = self._on_loop(lambda: self.router.dispatch(d))
        if hasattr(h, "code"):
            return self._dump(h)
        rec = self.s.coord.tasks.tasks[h.node_task_id]
        asyncio.run_coroutine_threadsafe(asyncio.wait_for(rec.done.wait(), WAIT_S), self.loop).result(WAIT_S + 30)
        return {**self._dump(h), "decision_ref": h.node_task_id}

    def result_of(self, handle: dict) -> dict:
        return self._on_loop(lambda: self.router.result_of(TaskHandle.model_validate({k: v for k, v in handle.items() if k != "decision_ref"})))

    def set_grader(self, task_id: str, spec: dict) -> None:
        self.router.set_grader(task_id, spec)

    def grade(self, task_id: str, output_ref: str) -> dict:
        return self._dump(self.router.grade(task_id, output_ref))  # runs in the worker: a code grade may take the sandbox's time

    def report_result(self, task_id: str, escalation_step: int, grader_result: dict) -> dict | None:
        out = self._on_loop(lambda: self.router.report_result(task_id, escalation_step, grader_result))
        return None if out is None else self._dump(out)

    def filter_outbound(self, intent: dict, entry_chat: dict) -> dict:
        return self.router.filter_outbound(intent, entry_chat)

    def record_owner_decision(self, parent_task_id: str, subtasks: list[dict], gesture: str) -> str:
        return self._on_loop(lambda: self.s.coord.record_owner_decision(parent_task_id, subtasks, gesture=gesture))

    def approve_decomposition(self, parent_task_id: str, subtasks: list[dict], owner_event_id: str) -> Any:
        return self._dump(self._on_loop(lambda: self.s.coord.approve_decomposition(parent_task_id, subtasks, owner_event_id)))

    def own_subtask(self, task_id: str, decomposition_id: str, subtask_id: str) -> str | None:
        return self._on_loop(lambda: self.s.coord.own_subtask(task_id, decomposition_id, subtask_id))


class ModelDirector:
    """The director as the coordinator's own model reading the board's view and answering one action. Which model it is comes from the coordinator's model profile (data); the reply is parsed here and
    VALIDATED by the board against its closed menu, so a model that answers something else gets a typed refusal and no progress."""

    SYSTEM_FILE = None

    def __init__(self, services, loop: asyncio.AbstractEventLoop) -> None:
        from envrec_net import envrun as E
        self.s, self.loop = services, loop
        self.system = (E.SKILLS_DIR / "harness-tools" / "references" / "director.txt").read_text(encoding="utf-8")

    def __call__(self, view: str) -> dict:
        import json as _json
        model = self.s.coord.runs._model()
        text = asyncio.run_coroutine_threadsafe(model.complete(self.system, view), self.loop).result(240).text
        a, b = text.find("{"), text.rfind("}")
        try:
            return _json.loads(text[a:b + 1])
        except ValueError:
            return {"action": "unparsed", "args": {}}  # not a menu action: the board refuses it with its typed reason and counts no progress
