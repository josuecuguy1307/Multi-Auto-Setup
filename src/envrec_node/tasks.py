"""What a node does with a task from its coordinator (docs/NETWORK_CONTRACT.md 3.7): check it, run it ONLY if it is on the read-only allowlist below, report the result.

THE ALLOWLIST IS THIS DICTIONARY AND NOTHING ELSE. A task type that is not a key of `ALLOWED` is rejected with a typed reason; there is no lookup by name, no import from a string, no
shell, no `eval`, and the task's parameters are validated against a Pydantic model that forbids undeclared fields (so a `command` or a path to write is "bad_parameters"). Writes need a
fresh approval from the owner ON the node, which this version does not offer: nothing here changes the machine (conformance T1 scans this module)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import envrec_os
from envrec_net import task as T
from envrec_os import probes
from envrec_node import analysis

MAX_CONCURRENT = 2


def _node_report(params: T.NodeReportParams, ctx: dict) -> tuple[dict, list[dict]]:
    """OS, the node process's own listening sockets (expected: none) and whether its identity key file is readable by its owner only."""
    sockets = envrec_os.own_listening_sockets()
    key = envrec_os.key_file_status(ctx["key_path"])
    return ({"os": envrec_os.os_info(), "sockets": sockets, "key": key, "agent": ctx.get("agent", "")},
            [{"provider": "envrec_os", "tool": "os_info"}, {"provider": "psutil", "tool": "own_listening_sockets", "count": len(sockets)}, {"provider": "envrec_os", "tool": "key_file_status"}])


def _machine_analysis(params: T.MachineAnalysisParams, ctx: dict) -> tuple[dict, list[dict]]:
    return analysis.run_analysis(params)


def _as_result(out: dict) -> tuple[dict, list[dict]]:
    """A probe of the per-OS layer as a task result: its facts and limits are the typed result, its evidence travels with it."""
    return {"facts": out["facts"], "limits": out["limits"]}, out["evidence"]


def _os_details(params: T.TaskParams, ctx: dict) -> tuple[dict, list[dict]]:
    return _as_result(probes.os_details())


def _installed_programs(params: T.TaskParams, ctx: dict) -> tuple[dict, list[dict]]:
    return _as_result(probes.installed_programs(limit=params.limit))  # type: ignore[attr-defined]


def _gpus(params: T.TaskParams, ctx: dict) -> tuple[dict, list[dict]]:
    return _as_result(probes.gpus())


def _model_runtimes(params: T.TaskParams, ctx: dict) -> tuple[dict, list[dict]]:
    return _as_result(probes.model_runtimes())


def _ai_clis(params: T.TaskParams, ctx: dict) -> tuple[dict, list[dict]]:
    return _as_result(probes.ai_clis())


def _memory_for_models(params: T.TaskParams, ctx: dict) -> tuple[dict, list[dict]]:
    return _as_result(probes.memory_for_models())


ALLOWED: dict[str, Callable[[T.TaskParams, dict], tuple[dict, list[dict]]]] = {
    "node_report": _node_report,
    "machine_analysis": _machine_analysis,
    "os_details": _os_details,
    "installed_programs": _installed_programs,
    "gpus": _gpus,
    "model_runtimes": _model_runtimes,
    "ai_clis": _ai_clis,
    "memory_for_models": _memory_for_models,
}

Send = Callable[[str, dict], Awaitable[None]]


class TaskRunner:
    def __init__(self, *, key_path, agent: str = "", max_concurrent: int = MAX_CONCURRENT, mono: Callable[[], float] = time.monotonic) -> None:
        self.ctx = {"key_path": key_path, "agent": agent}
        self.max_concurrent, self.mono = max_concurrent, mono
        self.checker: T.TokenChecker | None = None
        self._running: dict[str, dict] = {}

    def new_session(self, checker: T.TokenChecker) -> None:
        self.checker, self._running = checker, {}

    @property
    def running(self) -> int:
        return len(self._running)

    async def on_task(self, payload: dict, send: Send) -> None:
        received = self.mono()
        task_id = str(payload.get("task_id", ""))[:64]

        async def reject(reason: str) -> None:
            await send("task_update", {"task_id": task_id, "state": "rejected", "reason": reason})

        task_type = payload.get("task_type")
        if not isinstance(task_type, str) or task_type not in ALLOWED:
            return await reject("tool_not_allowed")  # not on the read-only list: a write, a shell command or anything else is refused here
        if len(self._running) >= self.max_concurrent:
            return await reject("busy")
        try:
            params = T.validate_params(task_type, payload.get("params") if isinstance(payload.get("params"), dict) else {"_": 1})
        except ValueError as exc:
            return await reject(str(exc))
        try:
            assert self.checker is not None
            token = self.checker.check(payload.get("token"), task_id=task_id, task_type=task_type)
        except T.TokenRejected as exc:
            return await reject(exc.reason)
        self._running[task_id] = {"cancelled": False}
        await send("task_update", {"task_id": task_id, "state": "accepted", "reason": ""})
        asyncio.ensure_future(self._run(task_id, task_type, params, token, received, send))

    async def _run(self, task_id: str, task_type: str, params, token: dict, received: float, send: Send) -> None:
        state = self._running[task_id]
        try:
            if self.mono() - received > float(token.get("ttl_s", T.DEFAULT_TTL_S)):
                await send("result", T.TaskResult(task_id=task_id, status="failed", reason="token_expired").model_dump())
                return
            await send("task_update", {"task_id": task_id, "state": "running", "reason": ""})
            t0 = self.mono()
            try:
                result, evidence = await asyncio.to_thread(ALLOWED[task_type], params, self.ctx)
                res = T.TaskResult(task_id=task_id, status="done", result=result, evidence=evidence, duration_s=round(self.mono() - t0, 2))
            except Exception as exc:  # a failing task is reported, never fatal to the node
                res = T.TaskResult(task_id=task_id, status="failed", reason=f"{type(exc).__name__}: {str(exc)[:120]}", duration_s=round(self.mono() - t0, 2))
            if state["cancelled"]:
                return  # the coordinator cancelled it and was already told
            await send("result", res.model_dump())
        finally:
            self._running.pop(task_id, None)

    async def on_cancel(self, payload: dict, send: Send) -> None:
        task_id = str(payload.get("task_id", ""))[:64]
        st = self._running.get(task_id)
        if st is not None:
            st["cancelled"] = True  # a task already running in its worker thread finishes on its own; its result is dropped
            await send("result", T.TaskResult(task_id=task_id, status="cancelled", reason="cancelled by the coordinator").model_dump())
