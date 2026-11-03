"""A model adapter whose calls go to a cloud access THROUGH the node that holds the credential (`JudgeTools._judge_ask`): the same guards as any other call from the coordinator to that access: the owner's recorded
answer on cloud use must allow it, the work must be public or internal, the node records a proof of execution, and every call reports the model id the API returned and its tokens. The coordinator holds no key."""

from __future__ import annotations

import time

from envrec.contracts import ModelProfile
from envrec.model.base import Completion, ModelAdapter, ModelError
from envrec_coordinator.slots import SlotRefused, load_slots


class NodeModel(ModelAdapter):
    def __init__(self, services, *, level: str = "public", label: str = "node") -> None:
        data = load_slots()
        super().__init__(ModelProfile(name=f"node:{data['escalation']['environment_id']}", kind="scripted", model_id=data["escalation"]["environment_id"], endpoint="scripted://node", supports_tool_calling=False,
                                      context_tokens=32768, view_budget_facts=24, menu_max=8))
        self.services, self.level, self.data, self.n, self.rid = services, level, data, 0, f"hj-{label}-{int(time.time())}"

    async def complete(self, system: str, user: str) -> Completion:
        answer, _ = self.services.coord.cloud_use_answer()
        if answer is None or answer in self.data["authority"]["refused_values"]:
            raise ModelError("the owner's recorded answer on cloud use does not allow asking the cloud")
        if self.level == "confidential":
            raise ModelError("confidential work never leaves the owner's computers")
        self.n += 1
        t0 = time.monotonic()
        try:
            got = await self.services._judge_ask(self.rid, self.n, self.data, system + "\n\n" + user, self.level, float(self.data["escalation"]["timeout_s"]))
        except SlotRefused as exc:
            raise ModelError(f"{exc.code}: {exc.detail}"[:200]) from None
        s = got["summary"] or {}
        usage = s.get("usage") or {}
        return Completion(text=got["text"], prompt_tokens=int(usage.get("prompt_tokens") or 0), completion_tokens=int(usage.get("completion_tokens") or 0), latency_s=time.monotonic() - t0, model_returned=s.get("model_returned"))
