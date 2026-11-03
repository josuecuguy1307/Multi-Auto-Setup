"""The recommender's model slots, wired to this coordinator (mixed into `Services`): the first model is the harness model; a second opinion goes to the cloud access through the NODE that holds it, as an
environment run with the same guards, owner origin and node-recorded proof as any task. The coordinator holds no key and reads none. What the owner allowed is his recorded answer on cloud use."""

from __future__ import annotations

import asyncio
import secrets

from envrec.model import load_profile, make_adapter
from envrec_coordinator.router import EntryChat, Refusal, Route, Task
from envrec_coordinator.router.catalog import build_candidates
from envrec_coordinator.slots import Slots, SlotRefused, load_slots

BUSY_RETRIES, BUSY_WAIT_S = 6, 3.0


class JudgeTools:
    def new_slots(self, *, level: str, task_text: str = "", local=None) -> Slots:
        data = load_slots()
        rid = "hj-" + secrets.token_hex(4)
        n = {"i": 0}

        async def remote(prompt: str, timeout_s: float) -> dict:
            n["i"] += 1
            return await self._judge_ask(rid, n["i"], data, prompt, level, timeout_s)

        def first(profile_name):
            if profile_name:
                profile, options = load_profile(self.coord.runs.models_file, profile_name)
                return make_adapter(profile, options)
            return local() if local is not None else self._recommend_model()

        return Slots(data, local=first, remote=remote, level=level, permit=self._judge_permit, log=lambda t, p: self.coord.log.add(t, p), task_text=task_text, clock=self.coord.clock)

    def _judge_permit(self) -> tuple[str | None, str]:
        answer, _ = self.coord.cloud_use_answer()
        return answer, ("stance_unanswered" if answer is None else "")

    def _judge_route(self, data: dict) -> Route | None:
        probe = Task(task_id="hj-probe", task_type=data["escalation"]["task_type"], sensitivity="public", entry_chat=EntryChat(kind="local", name="recommender"), created_by="OWNER", payload_ref="judge")
        cands, _ = build_candidates(probe, self.envs, self.facts, self.policy, {})
        machines = {m.machine_id: m for m in self.facts.machines()}
        for c in cands:
            if c.route.environment_id == data["escalation"]["environment_id"] and machines.get(c.route.machine_id) is not None and machines[c.route.machine_id].online:
                return c.route
        return None

    async def _judge_ask(self, rid: str, n: int, data: dict, prompt: str, level: str, timeout_s: float) -> dict:
        route = self._judge_route(data)
        if route is None:
            raise SlotRefused("no_route")
        task = Task(task_id=f"{rid}:{n}", task_type=data["escalation"]["task_type"], sensitivity=level, entry_chat=EntryChat(kind="local", name="recommender"), created_by="OWNER", payload_ref="judge")
        for attempt in range(BUSY_RETRIES + 1):
            handle = self.router.run_prompt(task, route, prompt)
            if isinstance(handle, Refusal):
                if handle.code == "busy" and attempt < BUSY_RETRIES:
                    await asyncio.sleep(BUSY_WAIT_S)
                    continue
                raise SlotRefused(handle.code)
            rec = self.coord.tasks.tasks[handle.node_task_id]
            try:
                await asyncio.wait_for(rec.done.wait(), timeout_s + 30)
            except TimeoutError:
                await self.coord.tasks.cancel(handle.node_task_id)
                raise SlotRefused("timed_out") from None
            if rec.reason == "busy" and attempt < BUSY_RETRIES:
                await asyncio.sleep(BUSY_WAIT_S)
                continue
            break
        if rec.status != "done":
            raise SlotRefused(rec.reason or rec.status)
        proof = self._proof_of(rec.result)
        if not proof["ok"]:
            raise SlotRefused("no_proof", proof["why"])  # an answer whose execution the node did not record is not used as a judgment
        return {"text": str(rec.result.get("output_text", "")), "summary": proof["summary"]}
